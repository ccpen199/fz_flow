from __future__ import annotations

import copy
import hashlib
import os
import re
import threading
import time
from uuid import uuid4

import pymysql

from integrations.llm_agent import LlmAgentClient
from packages.shared_contracts.python_models import FieldRole, SceneDTO, SceneFieldDTO, SceneRelationDTO


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


def _safe_fetch_schema() -> tuple[
    dict[str, list[str]],
    dict[str, dict[str, str]],
    list[dict],
    dict[str, dict[str, dict]],
    str | None,
]:
    mysql_cfg = _mysql_config()
    schema: dict[str, list[str]] = {}
    column_types: dict[str, dict[str, str]] = {}
    schema_tables_by_name: dict[str, dict] = {}
    column_metadata: dict[str, dict[str, dict]] = {}
    try:
        conn = pymysql.connect(**mysql_cfg)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      c.TABLE_NAME,
                      COALESCE(t.TABLE_COMMENT, '') AS TABLE_COMMENT,
                      c.COLUMN_NAME,
                      c.DATA_TYPE,
                      c.COLUMN_TYPE,
                      c.ORDINAL_POSITION,
                      c.IS_NULLABLE,
                      c.COLUMN_KEY,
                      COALESCE(c.COLUMN_COMMENT, '') AS COLUMN_COMMENT
                    FROM information_schema.columns c
                    LEFT JOIN information_schema.tables t
                      ON t.TABLE_SCHEMA = c.TABLE_SCHEMA
                     AND t.TABLE_NAME = c.TABLE_NAME
                    WHERE c.TABLE_SCHEMA = %s
                    ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
                    """,
                    (mysql_cfg["database"],),
                )
                for row in cur.fetchall():
                    table_name = str(row.get("TABLE_NAME", "")).strip()
                    column_name = str(row.get("COLUMN_NAME", "")).strip()
                    if not table_name or not column_name:
                        continue
                    table_comment = str(row.get("TABLE_COMMENT") or "").strip()
                    data_type = str(row.get("DATA_TYPE", "")).strip().lower()
                    column_type = str(row.get("COLUMN_TYPE", "")).strip().lower()
                    ordinal_position = int(row.get("ORDINAL_POSITION") or 0)
                    is_nullable = str(row.get("IS_NULLABLE") or "").strip().upper() == "YES"
                    is_primary = str(row.get("COLUMN_KEY") or "").strip().upper() == "PRI"
                    column_comment = str(row.get("COLUMN_COMMENT") or "").strip()
                    schema.setdefault(table_name, []).append(column_name)
                    if data_type:
                        column_types.setdefault(table_name, {})[column_name] = data_type
                    table_meta = schema_tables_by_name.setdefault(
                        table_name,
                        {
                            "table_name": table_name,
                            "table_comment": table_comment,
                            "table_role_hint": "",
                            "columns": [],
                        },
                    )
                    if table_comment and not table_meta.get("table_comment"):
                        table_meta["table_comment"] = table_comment
                    column_meta = {
                        "field_name": column_name,
                        "column_name": column_name,
                        "data_type": data_type,
                        "field_type": data_type,
                        "column_type": column_type or data_type,
                        "ordinal_position": ordinal_position,
                        "is_nullable": is_nullable,
                        "is_primary": is_primary,
                        "column_comment": column_comment,
                    }
                    table_meta["columns"].append(column_meta)
                    column_metadata.setdefault(table_name, {})[column_name] = column_meta
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return {}, {}, [], {}, str(exc)

    schema_tables = list(schema_tables_by_name.values())
    for table_meta in schema_tables:
        table_meta["table_role_hint"] = _guess_table_role(table_meta)
    return schema, column_types, schema_tables, column_metadata, None


def _safe_fetch_foreign_keys() -> tuple[list[dict], str | None]:
    mysql_cfg = _mysql_config()
    foreign_keys: list[dict] = []
    try:
        conn = pymysql.connect(**mysql_cfg)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                      kcu.TABLE_NAME AS child_table,
                      kcu.COLUMN_NAME AS child_column,
                      kcu.REFERENCED_TABLE_NAME AS parent_table,
                      kcu.REFERENCED_COLUMN_NAME AS parent_column
                    FROM information_schema.KEY_COLUMN_USAGE kcu
                    WHERE kcu.TABLE_SCHEMA = %s
                      AND kcu.REFERENCED_TABLE_NAME IS NOT NULL
                      AND kcu.REFERENCED_COLUMN_NAME IS NOT NULL
                    ORDER BY kcu.TABLE_NAME, kcu.COLUMN_NAME
                    """,
                    (mysql_cfg["database"],),
                )
                for row in cur.fetchall():
                    child_table = str(row.get("child_table", "")).strip()
                    child_column = str(row.get("child_column", "")).strip()
                    parent_table = str(row.get("parent_table", "")).strip()
                    parent_column = str(row.get("parent_column", "")).strip()
                    if not all([child_table, child_column, parent_table, parent_column]):
                        continue
                    foreign_keys.append(
                        {
                            "child_table": child_table,
                            "child_column": child_column,
                            "parent_table": parent_table,
                            "parent_column": parent_column,
                        }
                    )
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return [], str(exc)
    return foreign_keys, None


def _clean_text(value: object) -> str:
    return str(value or "").strip()


def _lower_blob(*values: object) -> str:
    return " ".join(_clean_text(value).lower() for value in values if _clean_text(value))


def _tokenize_business_text(*values: object) -> list[str]:
    raw = _lower_blob(*values)
    if not raw:
        return []
    tokens = [
        token
        for token in re.split(r"[^0-9a-zA-Z_\u4e00-\u9fff]+", raw)
        if len(token.strip()) >= 2
    ]
    seen: set[str] = set()
    deduped: list[str] = []
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        deduped.append(token)
    return deduped[:80]


def _guess_table_role(table_meta: dict) -> str:
    table_name = _clean_text(table_meta.get("table_name")).lower()
    table_comment = _clean_text(table_meta.get("table_comment")).lower()
    columns = table_meta.get("columns", []) if isinstance(table_meta.get("columns"), list) else []
    column_names = {str(item.get("field_name") or item.get("column_name") or "").strip().lower() for item in columns if isinstance(item, dict)}
    blob = _lower_blob(table_name, table_comment, " ".join(sorted(column_names)))

    if any(token in blob for token in ("dict", "dictionary", "dim", "dimension", "lookup", "enum", "master", "reference", "字典", "维表", "主数据", "枚举")):
        return "dictionary"
    if any(token in table_name for token in ("rel_", "_rel", "map_", "_map", "mapping", "link", "bridge", "关系", "映射")):
        return "relation"
    if any(token in blob for token in ("fact", "transaction", "order", "detail", "record", "log", "流水", "明细", "记录", "订单", "事实")):
        return "fact"
    if {"id", "name"}.issubset(column_names) or {"code", "name"}.issubset(column_names):
        return "master_data"
    if any(name.endswith("id") for name in column_names) and len(column_names) <= 8:
        return "lookup_candidate"
    return "business_table"


def _guess_field_role_hint(field_name: str, column_comment: str = "") -> str:
    blob = _lower_blob(field_name, column_comment)
    if any(token in blob for token in ("name", "title", "label", "display", "名称", "名字", "标题", "标签")):
        return "display_value"
    if any(token in blob for token in ("code", "key", "no", "number", "编码", "代码", "编号")):
        return "business_key"
    if any(token in blob for token in ("id", "主键")):
        return "id_key"
    if any(token in blob for token in ("type", "status", "category", "class", "group", "flag", "类型", "状态", "类目", "分类", "分组", "标记")):
        return "controlled_dimension"
    if any(token in blob for token in ("date", "time", "dt", "时间", "日期")):
        return "time"
    if any(token in blob for token in ("price", "amount", "qty", "count", "rate", "金额", "价格", "数量", "比例", "率")):
        return "measure"
    return ""


def _schema_table_map(schema_tables: list[dict]) -> dict[str, dict]:
    return {
        str(item.get("table_name", "")).strip(): item
        for item in schema_tables
        if isinstance(item, dict) and str(item.get("table_name", "")).strip()
    }


def _field_meta(column_metadata: dict[str, dict[str, dict]], table_name: str, field_name: str) -> dict:
    table_meta = column_metadata.get(table_name, {}) if isinstance(column_metadata, dict) else {}
    if field_name in table_meta:
        return table_meta[field_name]
    lower_name = field_name.lower()
    for key, value in table_meta.items():
        if key.lower() == lower_name and isinstance(value, dict):
            return value
    return {}


def _field_display_type(meta: dict, fallback: str = "") -> str:
    return _clean_text(meta.get("column_type") or meta.get("field_type") or meta.get("data_type") or fallback).lower()


def _build_dictionary_table_hints(schema_tables: list[dict], limit: int = 40) -> list[dict]:
    hints: list[dict] = []
    for table_meta in schema_tables:
        if not isinstance(table_meta, dict):
            continue
        table_name = _clean_text(table_meta.get("table_name"))
        table_role_hint = _clean_text(table_meta.get("table_role_hint"))
        columns = table_meta.get("columns", []) if isinstance(table_meta.get("columns"), list) else []
        if table_role_hint not in {"dictionary", "master_data", "lookup_candidate"}:
            continue
        label_columns: list[str] = []
        key_columns: list[str] = []
        control_columns: list[str] = []
        for column in columns:
            if not isinstance(column, dict):
                continue
            field_name = _clean_text(column.get("field_name") or column.get("column_name"))
            role_hint = _guess_field_role_hint(field_name, _clean_text(column.get("column_comment")))
            if role_hint == "display_value":
                label_columns.append(field_name)
            elif role_hint in {"business_key", "id_key"}:
                key_columns.append(field_name)
            elif role_hint == "controlled_dimension":
                control_columns.append(field_name)
        hints.append(
            {
                "table_name": table_name,
                "table_comment": _clean_text(table_meta.get("table_comment")),
                "table_role_hint": table_role_hint,
                "label_columns": label_columns[:5],
                "key_columns": key_columns[:5],
                "control_columns": control_columns[:5],
                "reason": "表名/注释/字段形态显示它可能是标准值、字典或主数据来源",
            }
        )
    return hints[:limit]


def _extract_table_candidate_names(raw_tables: object) -> list[str]:
    if not isinstance(raw_tables, list):
        return []
    names: list[str] = []
    for item in raw_tables:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = _clean_text(item.get("table_name") or item.get("name") or item.get("table"))
        else:
            name = ""
        if name:
            names.append(name)
    return names


def _table_id_name_variants(table_name: str) -> set[str]:
    compact = re.sub(r"[^0-9a-zA-Z]+", "", table_name).lower()
    variants = {f"{compact}id"} if compact else set()
    for suffix in ("info", "data", "detail", "details", "master", "dict", "dim", "table"):
        if compact.endswith(suffix) and len(compact) > len(suffix):
            variants.add(f"{compact[: -len(suffix)]}id")
    if compact.endswith("s") and len(compact) > 1:
        variants.add(f"{compact[:-1]}id")
    return {item for item in variants if item and item != "id"}


def _build_business_context(scene: SceneDTO, goal: str) -> dict:
    sample_goals = [str(item).strip() for item in getattr(scene, "sample_goals", []) if str(item).strip()]
    existing_fields = [
        {
            "table_name": item.table_name,
            "field_name": item.field_name,
            "semantic_name": item.semantic_name,
            "description": item.description,
            "role": item.role.value if hasattr(item.role, "value") else str(item.role),
            "enabled": item.enabled,
        }
        for item in scene.fields
    ]
    existing_relations = [
        {
            "left_table": item.left_table,
            "left_field": item.left_field,
            "right_table": item.right_table,
            "right_field": item.right_field,
            "join_type": item.join_type,
            "note": item.note,
        }
        for item in scene.relations
    ]
    analysis_goal = goal.strip() or "；".join(sample_goals[:3]) or scene.name
    return {
        "scene_name": scene.name,
        "scene_description": scene.description,
        "business_context": scene.description,
        "analysis_goal": analysis_goal,
        "user_goal": goal.strip(),
        "sample_goals": sample_goals,
        "existing_fields": existing_fields,
        "existing_relations": existing_relations,
    }


def _select_schema_table_details(
    schema_tables: list[dict],
    table_names: list[str],
    *,
    max_tables: int,
    max_fields_per_table: int,
) -> list[dict]:
    table_map = _schema_table_map(schema_tables)
    ordered_names: list[str] = []
    for name in table_names:
        if name in table_map and name not in ordered_names:
            ordered_names.append(name)
    if len(ordered_names) < max_tables:
        for item in schema_tables:
            name = _clean_text(item.get("table_name")) if isinstance(item, dict) else ""
            if name and name not in ordered_names:
                ordered_names.append(name)
            if len(ordered_names) >= max_tables:
                break

    details: list[dict] = []
    for name in ordered_names[:max_tables]:
        raw = copy.deepcopy(table_map.get(name, {}))
        if not raw:
            continue
        columns = raw.get("columns", []) if isinstance(raw.get("columns"), list) else []
        raw["columns"] = columns[:max_fields_per_table]
        details.append(raw)
    return details


def _build_schema_summary(schema_tables: list[dict], relation_candidates: list[dict], dictionary_hints: list[dict]) -> dict:
    role_names = sorted(
        {
            _clean_text(item.get("table_role_hint"))
            for item in schema_tables
            if isinstance(item, dict) and _clean_text(item.get("table_role_hint"))
        }
    )
    return {
        "table_count": len(schema_tables),
        "field_count": sum(len(item.get("columns", []) or []) for item in schema_tables if isinstance(item, dict)),
        "relation_candidate_count": len(relation_candidates),
        "dictionary_table_count": len(dictionary_hints),
        "table_role_counts": {
            role: sum(
                1
                for item in schema_tables
                if isinstance(item, dict) and _clean_text(item.get("table_role_hint")) == role
            )
            for role in role_names
        },
    }


def _sql_ident(name: str) -> str:
    return f"`{str(name).replace('`', '``')}`"


def _stable_candidate_id(prefix: str, *parts: str) -> str:
    raw = "|".join([prefix, *[str(part).strip().lower() for part in parts]])
    digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]  # noqa: S324
    return f"{prefix}_{digest}"


def _to_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _clamp_confidence(value: object, default: float = 0.5) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, num))


def _safe_estimate_join_hit_rate(
    left_table: str,
    left_field: str,
    right_table: str,
    right_field: str,
    sample_size: int = 500,
) -> tuple[float | None, int, str | None]:
    mysql_cfg = _mysql_config()
    try:
        conn = pymysql.connect(**mysql_cfg)
        try:
            with conn.cursor() as cur:
                query = f"""
                    SELECT
                      COUNT(*) AS sampled_rows,
                      SUM(
                        CASE
                          WHEN rt.{_sql_ident(right_field)} IS NOT NULL
                           AND lt.{_sql_ident(left_field)} IS NOT NULL
                          THEN 1 ELSE 0
                        END
                      ) AS matched_rows
                    FROM (
                      SELECT {_sql_ident(right_field)}
                      FROM {_sql_ident(right_table)}
                      WHERE {_sql_ident(right_field)} IS NOT NULL
                      LIMIT %s
                    ) AS rt
                    LEFT JOIN {_sql_ident(left_table)} lt
                      ON lt.{_sql_ident(left_field)} = rt.{_sql_ident(right_field)}
                """
                cur.execute(query, (sample_size,))
                row = cur.fetchone() or {}
                sampled_rows = int(row.get("sampled_rows") or 0)
                matched_rows = int(row.get("matched_rows") or 0)
                if sampled_rows <= 0:
                    return None, 0, None
                return matched_rows / sampled_rows, sampled_rows, None
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return None, 0, str(exc)


def _build_relation_confidence(
    relation: dict,
    foreign_key_set: set[tuple[str, str, str, str]],
) -> tuple[float, str]:
    left_table = relation.get("left_table", "")
    left_field = relation.get("left_field", "")
    right_table = relation.get("right_table", "")
    right_field = relation.get("right_field", "")
    fk_key = (
        right_table.lower(),
        right_field.lower(),
        left_table.lower(),
        left_field.lower(),
    )
    is_fk = fk_key in foreign_key_set

    hit_rate, sampled_rows, sample_error = _safe_estimate_join_hit_rate(
        left_table=left_table,
        left_field=left_field,
        right_table=right_table,
        right_field=right_field,
    )
    components: list[float] = [0.85 if is_fk else 0.4]
    reasons: list[str] = ["FK约束匹配" if is_fk else "启发式关系"]
    if sample_error:
        reasons.append(f"样本命中率计算失败: {sample_error}")
    elif hit_rate is None:
        reasons.append("右表样本为空")
    else:
        components.append(hit_rate)
        reasons.append(f"样本命中率={hit_rate:.2f} (n={sampled_rows})")
    confidence = sum(components) / len(components)
    return _clamp_confidence(confidence, default=0.5), "；".join(reasons)


def _guess_role(field_name: str) -> FieldRole:
    lower = field_name.lower()
    if any(key in lower for key in ("time", "date", "dt", "created", "updated")):
        return FieldRole.TIME
    if any(key in lower for key in ("price", "amount", "num", "count", "score", "rate", "qty")):
        return FieldRole.METRIC
    if any(key in lower for key in ("id", "flag", "status", "type")):
        return FieldRole.FILTER
    return FieldRole.DIMENSION


def _semantic_name(field_name: str) -> str:
    return field_name.strip() or "unknown_field"


def _build_schema_hint_candidates(
    scene: SceneDTO,
    schema: dict[str, list[str]],
    column_types: dict[str, dict[str, str]],
    foreign_keys: list[dict],
    max_tables: int,
    max_fields: int,
    schema_tables: list[dict] | None = None,
    column_metadata: dict[str, dict[str, dict]] | None = None,
    goal: str = "",
) -> dict:
    existing_tables = {item.table_name for item in scene.fields}
    schema_tables = schema_tables or []
    column_metadata = column_metadata or {}
    table_meta_map = _schema_table_map(schema_tables)
    table_pool = list(schema.keys())
    business_tokens = _tokenize_business_text(scene.name, scene.description, goal, *getattr(scene, "sample_goals", []))

    def score_table(table: str) -> int:
        score = 0
        lower = table.lower()
        table_meta = table_meta_map.get(table, {})
        columns = table_meta.get("columns", []) if isinstance(table_meta.get("columns"), list) else []
        table_blob = _lower_blob(
            table,
            table_meta.get("table_comment", ""),
            " ".join(
                _lower_blob(
                    item.get("field_name", ""),
                    item.get("column_comment", ""),
                )
                for item in columns
                if isinstance(item, dict)
            ),
        )
        if table in existing_tables:
            score += 50
        for token in business_tokens:
            if token and token in table_blob:
                score += 14
        if _clean_text(table_meta.get("table_comment")):
            score += 3
        table_role_hint = _clean_text(table_meta.get("table_role_hint"))
        if table_role_hint in {"dictionary", "master_data"}:
            score += 4
        if "info" in lower:
            score += 5
        if "scene" in lower:
            score += 5
        return score

    ranked_tables = sorted(table_pool, key=lambda t: score_table(t), reverse=True)[:max_tables]
    table_candidates = []
    for table in ranked_tables:
        table_meta = table_meta_map.get(table, {})
        table_candidates.append(
            {
                "table_name": table,
                "table_comment": _clean_text(table_meta.get("table_comment")),
                "table_role_hint": _clean_text(table_meta.get("table_role_hint")) or "business_table",
                "field_count": len(schema.get(table, []) or []),
                "selected": True,
                "confidence": _clamp_confidence(0.55 + min(score_table(table), 45) / 100, default=0.65),
                "reason": "根据场景业务描述、表名、字段名和注释综合召回",
            }
        )

    field_candidates: list[dict] = []
    for table in ranked_tables:
        table_meta = table_meta_map.get(table, {})
        table_role_hint = _clean_text(table_meta.get("table_role_hint")) or "business_table"
        table_comment = _clean_text(table_meta.get("table_comment"))
        for col in schema.get(table, [])[:max_fields]:
            meta = _field_meta(column_metadata, table, col)
            role = _guess_role(col).value
            semantic_name = _semantic_name(col)
            column_comment = _clean_text(meta.get("column_comment"))
            field_type = _field_display_type(meta, column_types.get(table, {}).get(col, ""))
            field_role_hint = _guess_field_role_hint(col, column_comment)
            reason_parts = ["基于数据库schema自动推荐"]
            if column_comment:
                reason_parts.append(f"字段注释: {column_comment}")
            if table_comment:
                reason_parts.append(f"表注释: {table_comment}")
            if field_role_hint:
                reason_parts.append(f"字段形态: {field_role_hint}")
            if table_role_hint in {"dictionary", "master_data", "lookup_candidate"}:
                reason_parts.append(f"可能来自受控值源: {table_role_hint}")
            field_candidates.append(
                {
                    "candidate_id": _stable_candidate_id("fld", table, col, semantic_name),
                    "table_name": table,
                    "field_name": col,
                    "semantic_name": semantic_name,
                    "description": column_comment or f"auto from schema {table}.{col}",
                    "role": role,
                    "field_type": field_type,
                    "column_type": field_type,
                    "data_type": _clean_text(meta.get("data_type") or column_types.get(table, {}).get(col, "")).lower(),
                    "column_comment": column_comment,
                    "table_comment": table_comment,
                    "table_role_hint": table_role_hint,
                    "field_role_hint": field_role_hint,
                    "is_primary": bool(meta.get("is_primary", False)),
                    "is_nullable": bool(meta.get("is_nullable", True)),
                    "required": role in {"metric", "time"},
                    "selected": True,
                    "enabled": True,
                    "confidence": 0.65,
                    "reason": "；".join(reason_parts),
                }
            )

    relation_candidates: list[dict] = []
    foreign_key_set: set[tuple[str, str, str, str]] = set()
    for fk in foreign_keys:
        child_table = str(fk.get("child_table", "")).strip()
        child_column = str(fk.get("child_column", "")).strip()
        parent_table = str(fk.get("parent_table", "")).strip()
        parent_column = str(fk.get("parent_column", "")).strip()
        if not all([child_table, child_column, parent_table, parent_column]):
            continue
        foreign_key_set.add((child_table.lower(), child_column.lower(), parent_table.lower(), parent_column.lower()))
        if child_table in ranked_tables and parent_table in ranked_tables:
            relation_candidates.append(
                {
                    "candidate_id": _stable_candidate_id(
                        "rel",
                        parent_table,
                        parent_column,
                        child_table,
                        child_column,
                        "LEFT",
                    ),
                    "left_table": parent_table,
                    "left_field": parent_column,
                    "right_table": child_table,
                    "right_field": child_column,
                    "join_type": "LEFT",
                    "cardinality": "1:N",
                    "origin": "foreign_key",
                    "required": False,
                    "selected": True,
                    "reason": "来自数据库外键约束",
                }
            )

    for i, left_table in enumerate(ranked_tables):
        left_cols = {c.lower(): c for c in schema.get(left_table, [])}
        left_id = left_cols.get("id") or left_cols.get("Id".lower())
        if not left_id:
            continue
        fk_name_variants = _table_id_name_variants(left_table)
        for right_table in ranked_tables[i + 1 :]:
            right_cols_raw = schema.get(right_table, [])
            right_cols = {c.lower(): c for c in right_cols_raw}
            matched_fk_name = next((name for name in fk_name_variants if name in right_cols), "")
            if matched_fk_name:
                relation_candidates.append(
                    {
                        "candidate_id": _stable_candidate_id(
                            "rel",
                            left_table,
                            left_id,
                            right_table,
                            right_cols[matched_fk_name],
                            "LEFT",
                        ),
                        "left_table": left_table,
                        "left_field": left_id,
                        "right_table": right_table,
                        "right_field": right_cols[matched_fk_name],
                        "join_type": "LEFT",
                        "cardinality": "1:N",
                        "origin": "naming_heuristic",
                        "required": False,
                        "selected": True,
                        "reason": f"通过实体ID命名模式自动推断: {right_table}.{right_cols[matched_fk_name]} -> {left_table}.{left_id}",
                    }
                )

    relation_seen: set[str] = set()
    dedup_relations: list[dict] = []
    for relation in relation_candidates:
        key = _stable_candidate_id(
            "relkey",
            relation.get("left_table", ""),
            relation.get("left_field", ""),
            relation.get("right_table", ""),
            relation.get("right_field", ""),
            relation.get("join_type", "LEFT"),
        )
        if key in relation_seen:
            continue
        relation_seen.add(key)
        confidence, reason = _build_relation_confidence(relation, foreign_key_set)
        relation["confidence"] = confidence
        relation["reason"] = reason if not relation.get("reason") else f"{relation.get('reason')}；{reason}"
        relation["note"] = relation.get("note") or relation["reason"]
        dedup_relations.append(relation)

    return {
        "tables": table_candidates,
        "fields": field_candidates,
        "relations": dedup_relations,
        "metric_templates": [],
        "regression_questions": [],
    }


def _normalize_candidates(candidates: dict) -> dict:
    normalized = {
        "tables": [],
        "fields": [],
        "relations": [],
        "metric_templates": candidates.get("metric_templates", []) or [],
        "regression_questions": candidates.get("regression_questions", []) or [],
    }

    for table in candidates.get("tables", []) or []:
        if isinstance(table, str) and table.strip():
            normalized["tables"].append(
                {
                    "table_name": table.strip(),
                    "selected": True,
                    "confidence": 0.5,
                    "reason": "",
                }
            )
        elif isinstance(table, dict):
            table_name = _clean_text(table.get("table_name") or table.get("name") or table.get("table"))
            if not table_name:
                continue
            normalized["tables"].append(
                {
                    "table_name": table_name,
                    "table_comment": _clean_text(table.get("table_comment") or table.get("comment")),
                    "table_role_hint": _clean_text(table.get("table_role_hint") or table.get("role_hint")),
                    "field_count": int(table.get("field_count") or 0),
                    "selected": _to_bool(table.get("selected", True), default=True),
                    "confidence": _clamp_confidence(table.get("confidence", 0.5), default=0.5),
                    "reason": _clean_text(table.get("reason") or table.get("note")),
                }
            )

    semantic_targets: dict[str, tuple[str, str]] = {}
    for item in candidates.get("fields", []) or []:
        if not isinstance(item, dict):
            continue
        table_name = str(item.get("table_name", "")).strip()
        field_name = str(item.get("field_name", "")).strip()
        semantic_name = str(item.get("semantic_name", "")).strip() or field_name
        role = str(item.get("role", "dimension")).strip().lower()
        if role not in {"metric", "dimension", "time", "filter"}:
            role = "dimension"
        if not table_name or not field_name:
            continue
        semantic_key = semantic_name.lower()
        current_target = (table_name.lower(), field_name.lower())
        existing_target = semantic_targets.get(semantic_key)
        if existing_target and existing_target != current_target:
            semantic_name = f"{semantic_name}_{table_name}.{field_name}"
            semantic_key = semantic_name.lower()
        semantic_targets[semantic_key] = current_target
        normalized["fields"].append(
            {
                "candidate_id": str(item.get("candidate_id") or _stable_candidate_id("fld", table_name, field_name, semantic_name)),
                "table_name": table_name,
                "field_name": field_name,
                "semantic_name": semantic_name,
                "description": str(item.get("description", "")).strip(),
                "role": role,
                "field_type": str(item.get("field_type", item.get("column_type", ""))).strip().lower(),
                "column_type": str(item.get("column_type", item.get("field_type", ""))).strip().lower(),
                "data_type": str(item.get("data_type", item.get("field_type", ""))).strip().lower(),
                "column_comment": _clean_text(item.get("column_comment") or item.get("comment")),
                "table_comment": _clean_text(item.get("table_comment")),
                "table_role_hint": _clean_text(item.get("table_role_hint") or item.get("table_type")),
                "field_role_hint": _clean_text(item.get("field_role_hint") or item.get("value_role_hint")),
                "value_source_hint": item.get("value_source_hint") if isinstance(item.get("value_source_hint"), dict) else {},
                "is_primary": _to_bool(item.get("is_primary", False), default=False),
                "is_nullable": _to_bool(item.get("is_nullable", True), default=True),
                "required": _to_bool(item.get("required", False), default=False),
                "selected": _to_bool(item.get("selected", item.get("enabled", True)), default=True),
                "enabled": _to_bool(item.get("enabled", item.get("selected", True)), default=True),
                "confidence": _clamp_confidence(item.get("confidence", 0.5), default=0.5),
                "reason": str(item.get("reason", item.get("description", ""))).strip(),
            }
        )

    for item in candidates.get("relations", []) or []:
        if not isinstance(item, dict):
            continue
        left_table = str(item.get("left_table", "")).strip()
        left_field = str(item.get("left_field", "")).strip()
        right_table = str(item.get("right_table", "")).strip()
        right_field = str(item.get("right_field", "")).strip()
        if not all([left_table, left_field, right_table, right_field]):
            continue
        normalized["relations"].append(
            {
                "candidate_id": str(
                    item.get("candidate_id")
                    or _stable_candidate_id("rel", left_table, left_field, right_table, right_field, item.get("join_type", "LEFT"))
                ),
                "left_table": left_table,
                "left_field": left_field,
                "right_table": right_table,
                "right_field": right_field,
                "join_type": str(item.get("join_type", "LEFT")).strip().upper() or "LEFT",
                "cardinality": str(item.get("cardinality", "1:N")).strip().upper() or "1:N",
                "origin": _clean_text(item.get("origin") or item.get("source") or item.get("relation_source")),
                "required": _to_bool(item.get("required", False), default=False),
                "selected": _to_bool(item.get("selected", True), default=True),
                "confidence": _clamp_confidence(item.get("confidence", 0.5), default=0.5),
                "reason": str(item.get("reason", item.get("note", ""))).strip(),
                "note": str(item.get("note", item.get("reason", ""))).strip(),
            }
        )

    return normalized


def _build_field_type_list(fields: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    for item in fields:
        if not isinstance(item, dict):
            continue
        field_type = str(item.get("field_type", "")).strip().lower()
        if not field_type:
            continue
        counts[field_type] = counts.get(field_type, 0) + 1
    return [{"field_type": field_type, "count": count} for field_type, count in sorted(counts.items())]


class LlmAgentService:
    _cache_lock = threading.Lock()
    _schema_cache: dict[str, object] = {
        "fetched_at": 0.0,
        "schema": {},
        "column_types": {},
        "schema_tables": [],
        "column_metadata": {},
        "foreign_keys": [],
        "schema_error": None,
        "foreign_key_error": None,
        "last_refresh_at": 0.0,
        "last_refresh_error": None,
    }

    def __init__(self) -> None:
        self.client = LlmAgentClient()
        self.schema_cache_ttl_seconds = max(0, int(os.getenv("LLM_AGENT_SCHEMA_CACHE_TTL_SECONDS", "300")))

    def health(self) -> dict:
        return self.client.health()

    def _load_db_metadata(self, *, force_refresh: bool = False) -> dict:
        now = time.time()
        with self._cache_lock:
            fetched_at = float(self._schema_cache.get("fetched_at", 0.0) or 0.0)
            has_payload = bool(self._schema_cache.get("schema"))
            cache_age_seconds = max(0, int(now - fetched_at)) if fetched_at else None
            if (
                not force_refresh
                and has_payload
                and fetched_at
                and (now - fetched_at) <= self.schema_cache_ttl_seconds
            ):
                return {
                    "schema": copy.deepcopy(self._schema_cache.get("schema", {})),
                    "column_types": copy.deepcopy(self._schema_cache.get("column_types", {})),
                    "schema_tables": copy.deepcopy(self._schema_cache.get("schema_tables", [])),
                    "column_metadata": copy.deepcopy(self._schema_cache.get("column_metadata", {})),
                    "foreign_keys": copy.deepcopy(self._schema_cache.get("foreign_keys", [])),
                    "schema_error": self._schema_cache.get("schema_error"),
                    "foreign_key_error": self._schema_cache.get("foreign_key_error"),
                    "cache_hit": True,
                    "cache_age_seconds": cache_age_seconds,
                    "fetched_at": fetched_at,
                    "ttl_seconds": self.schema_cache_ttl_seconds,
                    "last_refresh_at": self._schema_cache.get("last_refresh_at"),
                    "last_refresh_error": self._schema_cache.get("last_refresh_error"),
                }

        schema, column_types, schema_tables, column_metadata, schema_error = _safe_fetch_schema()
        foreign_keys, foreign_key_error = _safe_fetch_foreign_keys()
        refresh_error = None
        if schema_error or foreign_key_error:
            refresh_error = "; ".join([item for item in [schema_error, foreign_key_error] if item])

        refreshed_at = time.time()
        with self._cache_lock:
            previous_schema = copy.deepcopy(self._schema_cache.get("schema", {}))
            previous_column_types = copy.deepcopy(self._schema_cache.get("column_types", {}))
            previous_schema_tables = copy.deepcopy(self._schema_cache.get("schema_tables", []))
            previous_column_metadata = copy.deepcopy(self._schema_cache.get("column_metadata", {}))
            previous_foreign_keys = copy.deepcopy(self._schema_cache.get("foreign_keys", []))
            previous_fetched_at = float(self._schema_cache.get("fetched_at", 0.0) or 0.0)

            # If DB refresh fails, keep last successful payload as fallback and expose refresh error.
            if not schema_error and schema:
                self._schema_cache["schema"] = schema
                self._schema_cache["column_types"] = column_types
                self._schema_cache["schema_tables"] = schema_tables
                self._schema_cache["column_metadata"] = column_metadata
                self._schema_cache["fetched_at"] = refreshed_at
            elif not previous_schema:
                self._schema_cache["schema"] = schema
                self._schema_cache["column_types"] = column_types
                self._schema_cache["schema_tables"] = schema_tables
                self._schema_cache["column_metadata"] = column_metadata
                self._schema_cache["fetched_at"] = refreshed_at

            if not foreign_key_error:
                self._schema_cache["foreign_keys"] = foreign_keys
                if float(self._schema_cache.get("fetched_at", 0.0) or 0.0) <= 0:
                    self._schema_cache["fetched_at"] = refreshed_at
            elif not previous_foreign_keys:
                self._schema_cache["foreign_keys"] = foreign_keys

            self._schema_cache["schema_error"] = schema_error
            self._schema_cache["foreign_key_error"] = foreign_key_error
            self._schema_cache["last_refresh_at"] = refreshed_at
            self._schema_cache["last_refresh_error"] = refresh_error

            merged_schema = copy.deepcopy(self._schema_cache.get("schema", previous_schema))
            merged_column_types = copy.deepcopy(self._schema_cache.get("column_types", previous_column_types))
            merged_schema_tables = copy.deepcopy(self._schema_cache.get("schema_tables", previous_schema_tables))
            merged_column_metadata = copy.deepcopy(self._schema_cache.get("column_metadata", previous_column_metadata))
            merged_foreign_keys = copy.deepcopy(self._schema_cache.get("foreign_keys", previous_foreign_keys))
            merged_fetched_at = float(self._schema_cache.get("fetched_at", previous_fetched_at) or 0.0)
            cache_age_seconds = max(0, int(refreshed_at - merged_fetched_at)) if merged_fetched_at else None
            return {
                "schema": merged_schema,
                "column_types": merged_column_types,
                "schema_tables": merged_schema_tables,
                "column_metadata": merged_column_metadata,
                "foreign_keys": merged_foreign_keys,
                "schema_error": schema_error,
                "foreign_key_error": foreign_key_error,
                "cache_hit": False,
                "cache_age_seconds": cache_age_seconds,
                "fetched_at": merged_fetched_at,
                "ttl_seconds": self.schema_cache_ttl_seconds,
                "last_refresh_at": refreshed_at,
                "last_refresh_error": refresh_error,
            }

    def refresh_schema_cache(self) -> dict:
        metadata = self._load_db_metadata(force_refresh=True)
        return {
            "ok": not bool(metadata.get("last_refresh_error")),
            "schema_tables": len(metadata.get("schema", {})),
            "foreign_keys": len(metadata.get("foreign_keys", [])),
            "cache_hit": metadata.get("cache_hit", False),
            "cache_ttl_seconds": metadata.get("ttl_seconds", self.schema_cache_ttl_seconds),
            "cache_age_seconds": metadata.get("cache_age_seconds"),
            "fetched_at": metadata.get("fetched_at"),
            "last_refresh_at": metadata.get("last_refresh_at"),
            "last_refresh_error": metadata.get("last_refresh_error"),
            "schema_error": metadata.get("schema_error"),
            "foreign_key_error": metadata.get("foreign_key_error"),
        }

    def schema_cache_status(self) -> dict:
        metadata = self._load_db_metadata(force_refresh=False)
        return {
            "ok": True,
            "schema_tables": len(metadata.get("schema", {})),
            "foreign_keys": len(metadata.get("foreign_keys", [])),
            "cache_hit": metadata.get("cache_hit", False),
            "cache_ttl_seconds": metadata.get("ttl_seconds", self.schema_cache_ttl_seconds),
            "cache_age_seconds": metadata.get("cache_age_seconds"),
            "fetched_at": metadata.get("fetched_at"),
            "last_refresh_at": metadata.get("last_refresh_at"),
            "last_refresh_error": metadata.get("last_refresh_error"),
            "schema_error": metadata.get("schema_error"),
            "foreign_key_error": metadata.get("foreign_key_error"),
        }

    def schema_snapshot(self) -> dict:
        metadata = self._load_db_metadata(force_refresh=False)
        schema = metadata.get("schema", {})
        column_types = metadata.get("column_types", {})
        schema_tables_detail = metadata.get("schema_tables", [])
        foreign_keys = metadata.get("foreign_keys", [])

        tables: list[dict] = []
        if isinstance(schema_tables_detail, list) and schema_tables_detail:
            for table_meta in schema_tables_detail:
                if not isinstance(table_meta, dict):
                    continue
                table_name = _clean_text(table_meta.get("table_name"))
                columns = table_meta.get("columns", []) if isinstance(table_meta.get("columns"), list) else []
                tables.append(
                    {
                        "table_name": table_name,
                        "table_comment": _clean_text(table_meta.get("table_comment")),
                        "table_role_hint": _clean_text(table_meta.get("table_role_hint")) or "business_table",
                        "fields": [
                            {
                                "field_name": _clean_text(column.get("field_name") or column.get("column_name")),
                                "field_type": _clean_text(column.get("field_type") or column.get("data_type")).lower(),
                                "column_type": _clean_text(column.get("column_type")).lower(),
                                "ordinal_position": int(column.get("ordinal_position") or 0),
                                "is_nullable": bool(column.get("is_nullable", True)),
                                "is_primary": bool(column.get("is_primary", False)),
                                "column_comment": _clean_text(column.get("column_comment")),
                                "field_role_hint": _guess_field_role_hint(
                                    _clean_text(column.get("field_name") or column.get("column_name")),
                                    _clean_text(column.get("column_comment")),
                                ),
                            }
                            for column in columns
                            if isinstance(column, dict)
                        ],
                    }
                )
        else:
            for table_name in sorted(schema.keys()):
                columns = schema.get(table_name, []) or []
                tables.append(
                    {
                        "table_name": table_name,
                        "table_comment": "",
                        "table_role_hint": "business_table",
                        "fields": [
                            {
                                "field_name": field_name,
                                "field_type": str(column_types.get(table_name, {}).get(field_name, "")).strip().lower(),
                                "column_type": str(column_types.get(table_name, {}).get(field_name, "")).strip().lower(),
                                "ordinal_position": idx + 1,
                                "is_nullable": True,
                                "is_primary": False,
                                "column_comment": "",
                                "field_role_hint": _guess_field_role_hint(field_name, ""),
                            }
                            for idx, field_name in enumerate(columns)
                        ],
                    }
                )

        relation_candidates = [
            {
                "left_table": _clean_text(fk.get("parent_table")),
                "left_field": _clean_text(fk.get("parent_column")),
                "right_table": _clean_text(fk.get("child_table")),
                "right_field": _clean_text(fk.get("child_column")),
                "join_type": "LEFT",
                "cardinality": "1:N",
                "origin": "foreign_key",
                "confidence": 0.9,
                "reason": "来自数据库外键约束",
            }
            for fk in foreign_keys
            if isinstance(fk, dict)
        ]
        dictionary_table_hints = _build_dictionary_table_hints(schema_tables_detail if isinstance(schema_tables_detail, list) else [])

        return {
            "ok": not bool(metadata.get("schema_error")),
            "tables": tables,
            "foreign_keys": foreign_keys,
            "relation_candidates": relation_candidates,
            "dictionary_table_hints": dictionary_table_hints,
            "schema_tables": len(tables),
            "schema_table_count": len(tables),
            "foreign_key_count": len(foreign_keys),
            "schema_summary": _build_schema_summary(
                schema_tables_detail if isinstance(schema_tables_detail, list) else [],
                relation_candidates,
                dictionary_table_hints,
            ),
            "cache_hit": metadata.get("cache_hit", False),
            "cache_ttl_seconds": metadata.get("ttl_seconds", self.schema_cache_ttl_seconds),
            "cache_age_seconds": metadata.get("cache_age_seconds"),
            "fetched_at": metadata.get("fetched_at"),
            "last_refresh_at": metadata.get("last_refresh_at"),
            "last_refresh_error": metadata.get("last_refresh_error"),
            "schema_error": metadata.get("schema_error"),
            "foreign_key_error": metadata.get("foreign_key_error"),
        }

    def recommend(
        self,
        *,
        scene: SceneDTO,
        goal: str,
        max_tables: int,
        max_fields_per_table: int,
    ) -> dict:
        metadata = self._load_db_metadata(force_refresh=False)
        schema = metadata.get("schema", {})
        column_types = metadata.get("column_types", {})
        schema_tables = metadata.get("schema_tables", [])
        column_metadata = metadata.get("column_metadata", {})
        foreign_keys = metadata.get("foreign_keys", [])
        schema_error = metadata.get("schema_error")
        foreign_key_error = metadata.get("foreign_key_error")
        schema_hints = _build_schema_hint_candidates(
            scene=scene,
            schema=schema,
            column_types=column_types,
            foreign_keys=foreign_keys,
            max_tables=max_tables,
            max_fields=max_fields_per_table,
            schema_tables=schema_tables if isinstance(schema_tables, list) else [],
            column_metadata=column_metadata if isinstance(column_metadata, dict) else {},
            goal=goal,
        )
        table_names = _extract_table_candidate_names(schema_hints.get("tables"))
        schema_table_details = _select_schema_table_details(
            schema_tables if isinstance(schema_tables, list) else [],
            table_names,
            max_tables=max(max_tables * 2, max_tables),
            max_fields_per_table=max_fields_per_table,
        )
        dictionary_table_hints = _build_dictionary_table_hints(schema_tables if isinstance(schema_tables, list) else [])
        business_context = _build_business_context(scene, goal)
        schema_summary = _build_schema_summary(
            schema_tables if isinstance(schema_tables, list) else [],
            schema_hints.get("relations", []) if isinstance(schema_hints.get("relations"), list) else [],
            dictionary_table_hints,
        )

        payload = {
            "scene": scene.model_dump(mode="json"),
            "goal": goal,
            "business_context": business_context,
            "analysis_goal": business_context["analysis_goal"],
            "schema": schema,
            "schema_column_types": column_types,
            "schema_tables": schema_table_details,
            "schema_summary": schema_summary,
            "foreign_keys": foreign_keys,
            "relation_candidates": schema_hints.get("relations", []),
            "dictionary_table_hints": dictionary_table_hints,
            "fallback_candidates": schema_hints,
            "instruction": "请基于业务输入、information_schema 元数据、关系候选和 fallback_candidates 推荐待人工筛选的 tables / fields / relations。",
        }

        provider_notes: list[str] = []
        try:
            llm_result = self.client.recommend(payload)
        except Exception as exc:  # noqa: BLE001
            llm_result = {
                "provider": "local",
                "mode": "schema_fallback",
                "candidates": schema_hints,
                "notes": [f"configuration recommendation LLM failed, used schema fallback: {exc}"],
            }

        remote_candidates = llm_result.get("candidates") if isinstance(llm_result, dict) else None
        if not isinstance(remote_candidates, dict) or not remote_candidates:
            raise RuntimeError("configuration recommendation LLM returned no candidates")
        candidates = remote_candidates
        provider = llm_result.get("provider", "http")
        mode = llm_result.get("mode", "remote")
        provider_notes.extend(llm_result.get("notes", []) or [])

        if schema_error:
            provider_notes.append(f"schema fetch failed, used scene-local hints: {schema_error}")
        if foreign_key_error:
            provider_notes.append(f"foreign key fetch failed: {foreign_key_error}")
        if metadata.get("cache_hit"):
            provider_notes.append(f"schema cache hit (age={metadata.get('cache_age_seconds')}s)")

        normalized = _normalize_candidates(candidates)
        field_type_list = _build_field_type_list(normalized["fields"])

        return {
            "recommendation_id": f"rec_{uuid4().hex[:12]}",
            "scene_id": scene.scene_id,
            "scene_version": scene.version,
            "provider": provider,
            "mode": mode,
            "goal": goal,
            "candidates": normalized,
            "field_type_list": field_type_list,
            "business_context": business_context,
            "schema_summary": schema_summary,
            "dictionary_table_hints": dictionary_table_hints,
            "notes": provider_notes,
        }

    def canonicalize_recommendation(self, *, scene: SceneDTO, recommendation: dict) -> dict:
        if not isinstance(recommendation, dict):
            raise ValueError("recommendation must be a JSON object")

        source = recommendation
        if "candidates" in recommendation:
            candidates = recommendation.get("candidates", {})
        else:
            candidates = recommendation

        if not isinstance(candidates, dict):
            raise ValueError("recommendation.candidates must be a JSON object")

        normalized = _normalize_candidates(candidates)
        notes = recommendation.get("notes", [])
        if not isinstance(notes, list):
            notes = [str(notes)]
        field_type_list = _build_field_type_list(normalized["fields"])

        return {
            "recommendation_id": str(recommendation.get("recommendation_id") or f"rec_{uuid4().hex[:12]}"),
            "scene_id": scene.scene_id,
            "scene_version": scene.version,
            "provider": str(source.get("provider", "reviewed")),
            "mode": str(source.get("mode", "review")),
            "goal": str(source.get("goal", "")).strip(),
            "candidates": normalized,
            "field_type_list": field_type_list,
            "notes": [str(item) for item in notes if str(item).strip()],
        }

    def validate_recommendation(self, *, scene: SceneDTO, recommendation: dict) -> dict:
        canonical = self.canonicalize_recommendation(scene=scene, recommendation=recommendation)
        candidates = canonical.get("candidates", {})
        metadata = self._load_db_metadata(force_refresh=False)
        schema = metadata.get("schema", {})
        schema_column_types = metadata.get("column_types", {})
        schema_error = metadata.get("schema_error")

        issues: list[dict] = []

        def add_issue(level: str, code: str, message: str, path: str) -> None:
            issues.append(
                {
                    "level": level,
                    "code": code,
                    "message": message,
                    "path": path,
                }
            )

        if schema_error:
            add_issue("warning", "schema_unavailable", f"schema fetch failed: {schema_error}", "schema")

        schema_tables = {table.lower(): table for table in schema.keys()}
        schema_columns = {
            table.lower(): {column.lower(): column for column in columns}
            for table, columns in schema.items()
        }

        fields = candidates.get("fields", [])
        relations = candidates.get("relations", [])
        selected_fields = [item for item in fields if _to_bool(item.get("selected", True), default=True)]
        selected_relations = [item for item in relations if _to_bool(item.get("selected", True), default=True)]

        if not selected_fields:
            add_issue("error", "empty_fields", "at least one selected candidate field is required", "candidates.fields")

        candidate_tables = set(_extract_table_candidate_names(candidates.get("tables", [])))
        for item in selected_fields:
            candidate_tables.add(item.get("table_name", ""))
        for item in selected_relations:
            candidate_tables.add(item.get("left_table", ""))
            candidate_tables.add(item.get("right_table", ""))
        candidate_tables = {table for table in candidate_tables if isinstance(table, str) and table.strip()}

        if schema and candidate_tables:
            for table in sorted(candidate_tables):
                if table.lower() not in schema_tables:
                    add_issue("error", "unknown_table", f"table not found in schema: {table}", f"candidates.tables.{table}")

        field_binding_seen: set[tuple[str, str]] = set()
        semantic_binding: dict[str, str] = {}
        for idx, item in enumerate(selected_fields):
            table_name = item.get("table_name", "").strip()
            field_name = item.get("field_name", "").strip()
            semantic_name = item.get("semantic_name", "").strip()
            role = item.get("role", "").strip().lower()
            field_path = f"candidates.fields[{idx}]"

            if role not in {"metric", "dimension", "time", "filter"}:
                add_issue("error", "invalid_role", f"invalid role: {role}", f"{field_path}.role")

            binding_key = (table_name.lower(), field_name.lower())
            if binding_key in field_binding_seen:
                add_issue(
                    "warning",
                    "duplicate_field_binding",
                    f"duplicate field binding: {table_name}.{field_name}",
                    field_path,
                )
            field_binding_seen.add(binding_key)

            if semantic_name:
                bound_target = f"{table_name}.{field_name}"
                existing = semantic_binding.get(semantic_name.lower())
                if existing and existing != bound_target:
                    add_issue(
                        "error",
                        "semantic_conflict",
                        f"semantic name maps to multiple fields: {semantic_name}",
                        f"{field_path}.semantic_name",
                    )
                else:
                    semantic_binding[semantic_name.lower()] = bound_target

            if schema:
                table_cols = schema_columns.get(table_name.lower())
                if not table_cols:
                    add_issue("error", "unknown_table", f"table not found in schema: {table_name}", f"{field_path}.table_name")
                elif field_name.lower() not in table_cols:
                    add_issue(
                        "error",
                        "unknown_field",
                        f"field not found in schema: {table_name}.{field_name}",
                        f"{field_path}.field_name",
                    )
                else:
                    expected_type = str(
                        schema_column_types.get(table_name, {}).get(table_cols[field_name.lower()], "")
                    ).strip().lower()
                    input_type = str(item.get("field_type", "")).strip().lower()
                    if expected_type and input_type and expected_type != input_type:
                        add_issue(
                            "warning",
                            "field_type_mismatch",
                            f"field_type mismatch for {table_name}.{field_name}: expected {expected_type}, got {input_type}",
                            f"{field_path}.field_type",
                        )

        relation_seen: set[tuple[str, str, str, str, str]] = set()
        linked_tables: dict[str, set[str]] = {}
        for idx, item in enumerate(selected_relations):
            left_table = item.get("left_table", "").strip()
            left_field = item.get("left_field", "").strip()
            right_table = item.get("right_table", "").strip()
            right_field = item.get("right_field", "").strip()
            join_type = item.get("join_type", "").strip().upper()
            relation_path = f"candidates.relations[{idx}]"

            if join_type not in {"INNER", "LEFT", "RIGHT", "FULL", "CROSS"}:
                add_issue("error", "invalid_join_type", f"invalid join_type: {join_type}", f"{relation_path}.join_type")

            relation_key = (
                left_table.lower(),
                left_field.lower(),
                right_table.lower(),
                right_field.lower(),
                join_type,
            )
            if relation_key in relation_seen:
                add_issue("warning", "duplicate_relation", "duplicate relation found", relation_path)
            relation_seen.add(relation_key)

            linked_tables.setdefault(left_table.lower(), set()).add(right_table.lower())
            linked_tables.setdefault(right_table.lower(), set()).add(left_table.lower())

            if schema:
                left_cols = schema_columns.get(left_table.lower())
                right_cols = schema_columns.get(right_table.lower())
                if not left_cols:
                    add_issue(
                        "error",
                        "unknown_table",
                        f"left table not found in schema: {left_table}",
                        f"{relation_path}.left_table",
                    )
                elif left_field.lower() not in left_cols:
                    add_issue(
                        "error",
                        "unknown_field",
                        f"left field not found in schema: {left_table}.{left_field}",
                        f"{relation_path}.left_field",
                    )
                if not right_cols:
                    add_issue(
                        "error",
                        "unknown_table",
                        f"right table not found in schema: {right_table}",
                        f"{relation_path}.right_table",
                    )
                elif right_field.lower() not in right_cols:
                    add_issue(
                        "error",
                        "unknown_field",
                        f"right field not found in schema: {right_table}.{right_field}",
                        f"{relation_path}.right_field",
                    )

        tables_from_fields = {
            item.get("table_name", "").strip().lower()
            for item in selected_fields
            if isinstance(item, dict) and item.get("table_name", "").strip()
        }
        if len(tables_from_fields) > 1:
            visited: set[str] = set()
            start = next(iter(tables_from_fields))
            queue = [start]
            while queue:
                table = queue.pop(0)
                if table in visited:
                    continue
                visited.add(table)
                queue.extend([n for n in linked_tables.get(table, set()) if n not in visited])
            disconnected = sorted(tables_from_fields - visited)
            if disconnected:
                add_issue(
                    "warning",
                    "relation_disconnected",
                    f"some field tables are disconnected from relation graph: {', '.join(disconnected)}",
                    "candidates.relations",
                )

        error_count = sum(1 for issue in issues if issue["level"] == "error")
        warning_count = sum(1 for issue in issues if issue["level"] == "warning")
        return {
            "ok": error_count == 0,
            "scene_id": scene.scene_id,
            "recommendation_id": canonical.get("recommendation_id"),
            "error_count": error_count,
            "warning_count": warning_count,
            "issues": issues,
            "canonical_recommendation": canonical,
        }

    def apply_to_scene(self, *, scene: SceneDTO, recommendation: dict, merge_mode: str = "append") -> dict:
        candidates = recommendation.get("candidates", {}) if isinstance(recommendation, dict) else {}
        fields = candidates.get("fields", []) if isinstance(candidates, dict) else []
        relations = candidates.get("relations", []) if isinstance(candidates, dict) else []
        selected_fields = [item for item in fields if _to_bool(item.get("selected", True), default=True)]
        selected_relations = [item for item in relations if _to_bool(item.get("selected", True), default=True)]

        if merge_mode == "replace":
            scene.fields = []
            scene.relations = []

        field_keys = {(f.table_name, f.field_name, f.semantic_name) for f in scene.fields}
        relation_keys = {
            (r.left_table, r.left_field, r.right_table, r.right_field, r.join_type)
            for r in scene.relations
        }

        added_fields = 0
        added_relations = 0

        for item in selected_fields:
            key = (item["table_name"], item["field_name"], item["semantic_name"])
            if key in field_keys:
                continue
            scene.fields.append(
                SceneFieldDTO(
                    field_id=f"field_{uuid4().hex[:10]}",
                    table_name=item["table_name"],
                    field_name=item["field_name"],
                    semantic_name=item["semantic_name"],
                    description=item.get("description", ""),
                    role=FieldRole(item.get("role", "dimension")),
                    enabled=bool(item.get("enabled", True)),
                )
            )
            field_keys.add(key)
            added_fields += 1

        for item in selected_relations:
            join_type = item.get("join_type", "LEFT").upper()
            key = (
                item["left_table"],
                item["left_field"],
                item["right_table"],
                item["right_field"],
                join_type,
            )
            if key in relation_keys:
                continue
            scene.relations.append(
                SceneRelationDTO(
                    relation_id=f"rel_{uuid4().hex[:10]}",
                    left_table=item["left_table"],
                    left_field=item["left_field"],
                    right_table=item["right_table"],
                    right_field=item["right_field"],
                    join_type=join_type,
                    note=item.get("note", ""),
                )
            )
            relation_keys.add(key)
            added_relations += 1

        return {
            "scene_id": scene.scene_id,
            "merge_mode": merge_mode,
            "added_fields": added_fields,
            "added_relations": added_relations,
            "total_fields": len(scene.fields),
            "total_relations": len(scene.relations),
        }
