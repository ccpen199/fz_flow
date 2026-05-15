from __future__ import annotations

import hashlib
import os
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


def _safe_fetch_schema() -> tuple[dict[str, list[str]], dict[str, dict[str, str]], str | None]:
    mysql_cfg = _mysql_config()
    schema: dict[str, list[str]] = {}
    column_types: dict[str, dict[str, str]] = {}
    try:
        conn = pymysql.connect(**mysql_cfg)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE
                    FROM information_schema.columns
                    WHERE table_schema = %s
                    ORDER BY TABLE_NAME, ORDINAL_POSITION
                    """,
                    (mysql_cfg["database"],),
                )
                for row in cur.fetchall():
                    table_name = row["TABLE_NAME"]
                    column_name = row["COLUMN_NAME"]
                    data_type = str(row.get("DATA_TYPE", "")).strip().lower()
                    schema.setdefault(table_name, []).append(column_name)
                    if data_type:
                        column_types.setdefault(table_name, {})[column_name] = data_type
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        return {}, {}, str(exc)
    return schema, column_types, None


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


def _build_heuristic_candidates(
    scene: SceneDTO,
    schema: dict[str, list[str]],
    column_types: dict[str, dict[str, str]],
    foreign_keys: list[dict],
    max_tables: int,
    max_fields: int,
) -> dict:
    existing_tables = {item.table_name for item in scene.fields}
    table_pool = list(schema.keys())

    def score_table(table: str) -> int:
        score = 0
        lower = table.lower()
        if table in existing_tables:
            score += 50
        for token in [scene.name, scene.description]:
            token_lower = token.lower().strip()
            if token_lower and token_lower in lower:
                score += 20
        if "info" in lower:
            score += 5
        if "scene" in lower:
            score += 5
        return score

    ranked_tables = sorted(table_pool, key=lambda t: score_table(t), reverse=True)[:max_tables]

    field_candidates: list[dict] = []
    for table in ranked_tables:
        for col in schema.get(table, [])[:max_fields]:
            role = _guess_role(col).value
            semantic_name = _semantic_name(col)
            field_candidates.append(
                {
                    "candidate_id": _stable_candidate_id("fld", table, col, semantic_name),
                    "table_name": table,
                    "field_name": col,
                    "semantic_name": semantic_name,
                    "description": f"auto from schema {table}.{col}",
                    "role": role,
                    "field_type": str(column_types.get(table, {}).get(col, "")).strip().lower(),
                    "required": role in {"metric", "time"},
                    "selected": True,
                    "enabled": True,
                    "confidence": 0.65,
                    "reason": "基于schema字段语义规则自动推荐",
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
                    "required": False,
                    "selected": True,
                    "reason": "来自数据库外键约束",
                }
            )

    for i, left_table in enumerate(ranked_tables):
        left_cols = {c.lower() for c in schema.get(left_table, [])}
        if "id" not in left_cols:
            continue
        for right_table in ranked_tables[i + 1 :]:
            right_cols_raw = schema.get(right_table, [])
            right_cols = {c.lower(): c for c in right_cols_raw}
            fk_name = f"{left_table.lower().replace('`', '')[:-5] if left_table.lower().endswith('_info') else left_table.lower()}id"
            if fk_name in right_cols:
                relation_candidates.append(
                    {
                        "candidate_id": _stable_candidate_id(
                            "rel",
                            left_table,
                            "Id" if "id" in left_cols else "id",
                            right_table,
                            right_cols[fk_name],
                            "LEFT",
                        ),
                        "left_table": left_table,
                        "left_field": "Id" if "id" in left_cols else "id",
                        "right_table": right_table,
                        "right_field": right_cols[fk_name],
                        "join_type": "LEFT",
                        "cardinality": "1:N",
                        "required": False,
                        "selected": True,
                        "reason": "通过 *_id 命名模式自动推断",
                    }
                )
            elif "clothingid" in right_cols and "id" in left_cols:
                relation_candidates.append(
                    {
                        "candidate_id": _stable_candidate_id(
                            "rel",
                            left_table,
                            "Id",
                            right_table,
                            right_cols["clothingid"],
                            "LEFT",
                        ),
                        "left_table": left_table,
                        "left_field": "Id",
                        "right_table": right_table,
                        "right_field": right_cols["clothingid"],
                        "join_type": "LEFT",
                        "cardinality": "1:N",
                        "required": False,
                        "selected": True,
                        "reason": "通过 ClothingId 命名模式自动推断",
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
        "tables": ranked_tables,
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
            normalized["tables"].append(table.strip())

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
                    "schema": dict(self._schema_cache.get("schema", {})),
                    "column_types": dict(self._schema_cache.get("column_types", {})),
                    "foreign_keys": list(self._schema_cache.get("foreign_keys", [])),
                    "schema_error": self._schema_cache.get("schema_error"),
                    "foreign_key_error": self._schema_cache.get("foreign_key_error"),
                    "cache_hit": True,
                    "cache_age_seconds": cache_age_seconds,
                    "fetched_at": fetched_at,
                    "ttl_seconds": self.schema_cache_ttl_seconds,
                    "last_refresh_at": self._schema_cache.get("last_refresh_at"),
                    "last_refresh_error": self._schema_cache.get("last_refresh_error"),
                }

        schema, column_types, schema_error = _safe_fetch_schema()
        foreign_keys, foreign_key_error = _safe_fetch_foreign_keys()
        refresh_error = None
        if schema_error or foreign_key_error:
            refresh_error = "; ".join([item for item in [schema_error, foreign_key_error] if item])

        refreshed_at = time.time()
        with self._cache_lock:
            previous_schema = dict(self._schema_cache.get("schema", {}))
            previous_column_types = dict(self._schema_cache.get("column_types", {}))
            previous_foreign_keys = list(self._schema_cache.get("foreign_keys", []))
            previous_fetched_at = float(self._schema_cache.get("fetched_at", 0.0) or 0.0)

            # If DB refresh fails, keep last successful payload as fallback and expose refresh error.
            if not schema_error and schema:
                self._schema_cache["schema"] = schema
                self._schema_cache["column_types"] = column_types
                self._schema_cache["fetched_at"] = refreshed_at
            elif not previous_schema:
                self._schema_cache["schema"] = schema
                self._schema_cache["column_types"] = column_types
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

            merged_schema = dict(self._schema_cache.get("schema", previous_schema))
            merged_column_types = dict(self._schema_cache.get("column_types", previous_column_types))
            merged_foreign_keys = list(self._schema_cache.get("foreign_keys", previous_foreign_keys))
            merged_fetched_at = float(self._schema_cache.get("fetched_at", previous_fetched_at) or 0.0)
            cache_age_seconds = max(0, int(refreshed_at - merged_fetched_at)) if merged_fetched_at else None
            return {
                "schema": merged_schema,
                "column_types": merged_column_types,
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
        foreign_keys = metadata.get("foreign_keys", [])

        tables: list[dict] = []
        for table_name in sorted(schema.keys()):
            columns = schema.get(table_name, []) or []
            tables.append(
                {
                    "table_name": table_name,
                    "fields": [
                        {
                            "field_name": field_name,
                            "field_type": str(column_types.get(table_name, {}).get(field_name, "")).strip().lower(),
                        }
                        for field_name in columns
                    ],
                }
            )

        return {
            "ok": not bool(metadata.get("schema_error")),
            "tables": tables,
            "foreign_keys": foreign_keys,
            "schema_tables": len(tables),
            "foreign_key_count": len(foreign_keys),
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
        foreign_keys = metadata.get("foreign_keys", [])
        schema_error = metadata.get("schema_error")
        foreign_key_error = metadata.get("foreign_key_error")
        heuristic = _build_heuristic_candidates(
            scene=scene,
            schema=schema,
            column_types=column_types,
            foreign_keys=foreign_keys,
            max_tables=max_tables,
            max_fields=max_fields_per_table,
        )

        payload = {
            "scene": scene.model_dump(mode="json"),
            "goal": goal,
            "schema": schema,
            "schema_column_types": column_types,
            "fallback_candidates": heuristic,
            "instruction": "请为场景剧本草拟推荐候选 tables / fields / relations，并包含字段类型列表。",
        }

        provider_notes: list[str] = []
        candidates = heuristic
        provider = "heuristic"
        mode = "local"

        try:
            llm_result = self.client.recommend(payload)
            remote_candidates = llm_result.get("candidates") if isinstance(llm_result, dict) else None
            if isinstance(remote_candidates, dict) and remote_candidates:
                candidates = remote_candidates
            provider = llm_result.get("provider", provider)
            mode = llm_result.get("mode", mode)
            provider_notes.extend(llm_result.get("notes", []) or [])
        except Exception as exc:  # noqa: BLE001
            provider_notes.append(f"llm provider fallback to heuristic: {exc}")

        if schema_error:
            provider_notes.append(f"schema fetch failed, used scene-local hints: {schema_error}")
        if foreign_key_error:
            provider_notes.append(f"foreign key fetch failed: {foreign_key_error}")
        if metadata.get("cache_hit"):
            provider_notes.append(f"schema cache hit (age={metadata.get('cache_age_seconds')}s)")

        normalized = _normalize_candidates(candidates)
        if not normalized["fields"] and scene.fields:
            normalized["fields"] = [
                {
                    "table_name": f.table_name,
                    "field_name": f.field_name,
                    "semantic_name": f.semantic_name,
                    "description": f.description,
                    "role": f.role.value,
                    "field_type": "",
                    "required": False,
                    "selected": bool(f.enabled),
                    "enabled": f.enabled,
                    "confidence": 0.8,
                    "reason": "来自当前场景已配置字段",
                }
                for f in scene.fields
            ]

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

        candidate_tables = set(candidates.get("tables", []))
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
