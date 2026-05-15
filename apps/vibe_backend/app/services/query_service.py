from __future__ import annotations

import os
import re
import time
from uuid import uuid4

import pymysql
from packages.shared_contracts.python_models import QueryPlanDTO, QueryRunDTO, SceneDTO
from .semantic_field_cache_service import semantic_field_cache_service

ALLOWED_JOIN_TYPES = {"INNER", "LEFT", "RIGHT", "LEFT OUTER", "RIGHT OUTER"}
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
COUNT_SENTINEL = "__count__"
BLOCKED_SQL_KEYWORDS = re.compile(
    r"\b(insert|update|delete|replace|drop|alter|truncate|create|grant|revoke|merge|call|execute)\b",
    flags=re.IGNORECASE,
)
DOUBLE_QUOTED_ALIAS_RE = re.compile(r'\bAS\s+"([^"]+)"', flags=re.IGNORECASE)
DOUBLE_QUOTED_QUALIFIED_IDENTIFIER_RE = re.compile(r'(\b[A-Za-z_][A-Za-z0-9_]*\.)"([^"]+)"')
UNBOUND_PLACEHOLDER_SQL_RE = re.compile(
    r"(:[A-Za-z_][A-Za-z0-9_]*|\$\{[^}]+\}|<[A-Za-z_][A-Za-z0-9_\-\s]*>|\?|待确认|待补充)",
    flags=re.IGNORECASE,
)
NON_MYSQL_INTERVAL_LITERAL_RE = re.compile(r"\bINTERVAL\s+'[^']+'", flags=re.IGNORECASE)


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


def _mysql_quote_identifier(identifier: str) -> str:
    return f"`{str(identifier).replace('`', '``')}`"


def _normalize_mysql_identifier_quotes(sql: str) -> tuple[str, bool]:
    """Normalize common LLM-generated ANSI identifier quotes to MySQL backticks.

    The SQL agent sometimes emits `AS "中文别名"` and later `ORDER BY "中文别名"`.
    MySQL may treat the latter as a string literal, so the query succeeds but
    returns wrongly ordered business results.
    """
    sql_text = str(sql or "")
    aliases = {
        str(match.group(1) or "").strip()
        for match in DOUBLE_QUOTED_ALIAS_RE.finditer(sql_text)
        if str(match.group(1) or "").strip()
    }
    normalized = DOUBLE_QUOTED_ALIAS_RE.sub(
        lambda match: f"AS {_mysql_quote_identifier(match.group(1))}",
        sql_text,
    )
    normalized = DOUBLE_QUOTED_QUALIFIED_IDENTIFIER_RE.sub(
        lambda match: f"{match.group(1)}{_mysql_quote_identifier(match.group(2))}",
        normalized,
    )
    for alias in sorted(aliases, key=len, reverse=True):
        normalized = normalized.replace(f'"{alias}"', _mysql_quote_identifier(alias))
    return normalized, normalized != sql_text


def _get_mysql_schema(connection: pymysql.connections.Connection) -> dict[str, set[str]]:
    schema: dict[str, set[str]] = {}
    database_name = _mysql_config()["database"]
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT TABLE_NAME, COLUMN_NAME
            FROM information_schema.columns
            WHERE table_schema = %s
            """,
            (database_name,),
        )
        for row in cursor.fetchall():
            schema.setdefault(row["TABLE_NAME"], set()).add(row["COLUMN_NAME"])
    return schema


def _resolve_semantic_name(raw_name: str, alias_map: dict[str, str]) -> str:
    key = str(raw_name or "").strip()
    if not key:
        return ""
    return alias_map.get(key.lower(), key)


def _normalize_intent_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "").lower())


def _intent_has_any(intent_text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in intent_text for keyword in keywords)


def _intent_sort_direction(intent_text: str) -> str:
    if _intent_has_any(intent_text, ("升序", "asc", "lowest", "smallest", "最小")):
        return "ASC"
    return "DESC"


def _intent_top_n(intent: str, default: int = 0) -> int:
    text = str(intent or "")
    for pattern in (r"(?:前|top)\s*(\d+)", r"返回\s*(\d+)\s*条"):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        try:
            value = int(match.group(1))
        except ValueError:
            continue
        if value > 0:
            return min(value, 200)
    return default


def _field_match_score(field, normalized_intent: str) -> int:
    score = 0
    semantic_name = str(getattr(field, "semantic_name", "") or "").strip().lower()
    table_name = str(getattr(field, "table_name", "") or "").strip().lower()
    field_name = str(getattr(field, "field_name", "") or "").strip().lower()
    if semantic_name and semantic_name in normalized_intent:
        score += 8
    if field_name and field_name in normalized_intent:
        score += 6
    if table_name and table_name in normalized_intent:
        score += 2
    if semantic_name and any(part and part in normalized_intent for part in re.split(r"[_\W]+", semantic_name)):
        score += 2
    return score


def _pick_fields_from_intent(queryable_fields, global_goal: str) -> tuple[list[str], list[str]]:
    metric_fields = [field for field in queryable_fields if field.role == "metric" and field.enabled]
    dimension_fields = [field for field in queryable_fields if field.role in {"dimension", "time"} and field.enabled]
    normalized_intent = _normalize_intent_text(global_goal)

    if not normalized_intent:
        return (
            [field.semantic_name for field in metric_fields[:2]],
            [field.semantic_name for field in dimension_fields[:2]],
        )

    has_count_intent = _intent_has_any(normalized_intent, ("统计", "count", "数量", "个数", "数目", "多少"))
    has_avg_intent = _intent_has_any(normalized_intent, ("平均", "均价", "avg", "mean"))
    has_sum_intent = _intent_has_any(normalized_intent, ("总和", "总计", "sum", "合计"))
    has_max_intent = _intent_has_any(normalized_intent, ("最大", "最高", "max"))
    has_min_intent = _intent_has_any(normalized_intent, ("最小", "最低", "min"))

    scored_dims = sorted(
        ((field.semantic_name, _field_match_score(field, normalized_intent)) for field in dimension_fields),
        key=lambda item: item[1],
        reverse=True,
    )
    selected_dimensions = [name for name, score in scored_dims if score > 0][:2]
    if not selected_dimensions:
        selected_dimensions = [field.semantic_name for field in dimension_fields[:2]]

    selected_metrics: list[str] = []
    if has_count_intent and not (has_avg_intent or has_sum_intent or has_max_intent or has_min_intent):
        selected_metrics = [COUNT_SENTINEL]
    else:
        scored_metrics = sorted(
            ((field.semantic_name, _field_match_score(field, normalized_intent)) for field in metric_fields),
            key=lambda item: item[1],
            reverse=True,
        )
        selected_metrics = [name for name, score in scored_metrics if score > 0][:2]
        if not selected_metrics:
            selected_metrics = [field.semantic_name for field in metric_fields[:2]]

    return selected_metrics, selected_dimensions


def _metric_aggregation_from_intent(intent: str) -> str:
    normalized_intent = _normalize_intent_text(intent)
    if _intent_has_any(normalized_intent, ("平均", "均价", "avg", "mean")):
        return "avg"
    if _intent_has_any(normalized_intent, ("总和", "总计", "sum", "合计")):
        return "sum"
    if _intent_has_any(normalized_intent, ("最大", "最高", "max")):
        return "max"
    if _intent_has_any(normalized_intent, ("最小", "最低", "min")):
        return "min"
    return "avg"


def _queryable_context(scene: SceneDTO):
    base_fields = [field for field in scene.fields if field.enabled]
    base_alias_map: dict[str, str] = {}
    for field in base_fields:
        name = str(field.semantic_name or "").strip()
        if name:
            base_alias_map[name.lower()] = name
    try:
        cached_fields = semantic_field_cache_service.get_queryable_scene_fields(scene.scene_id)
        alias_map = semantic_field_cache_service.get_queryable_alias_map(scene.scene_id)
        if cached_fields:
            merged = list(cached_fields)
            seen_keys = {
                (
                    str(field.semantic_name or "").strip().lower(),
                    str(field.table_name or "").strip().lower(),
                    str(field.field_name or "").strip().lower(),
                )
                for field in cached_fields
            }
            for field in base_fields:
                dedupe_key = (
                    str(field.semantic_name or "").strip().lower(),
                    str(field.table_name or "").strip().lower(),
                    str(field.field_name or "").strip().lower(),
                )
                if dedupe_key in seen_keys:
                    continue
                seen_keys.add(dedupe_key)
                merged.append(field)
            return merged, {**base_alias_map, **alias_map}
    except Exception:  # noqa: BLE001
        pass
    return base_fields, base_alias_map


def _find_scene_field(queryable_fields, semantic_name: str, alias_map: dict[str, str]):
    target = _resolve_semantic_name(semantic_name, alias_map)
    if not target:
        return None
    for field in queryable_fields:
        if field.semantic_name == target:
            return field
    return None


def _collect_scene_whitelist(queryable_fields, relations) -> dict[str, set[str]]:
    whitelist: dict[str, set[str]] = {}
    for field in queryable_fields:
        whitelist.setdefault(field.table_name, set()).add(field.field_name)
    for relation in relations:
        whitelist.setdefault(relation.left_table, set()).add(relation.left_field)
        whitelist.setdefault(relation.right_table, set()).add(relation.right_field)
    return whitelist


def _validate_query_plan(scene: SceneDTO, query_plan: QueryPlanDTO, db_schema: dict[str, set[str]] | None = None) -> list[dict]:
    queryable_fields, alias_map = _queryable_context(scene)
    whitelist = _collect_scene_whitelist(queryable_fields, scene.relations)
    checks: list[dict] = []
    for semantic_name in (query_plan.dimensions or []) + (query_plan.metrics or []):
        if semantic_name == COUNT_SENTINEL:
            continue
        field = _find_scene_field(queryable_fields, semantic_name, alias_map)
        schema_passed = bool(field and field.field_name in db_schema.get(field.table_name, set())) if db_schema else True
        checks.append(
            {
                "type": "field_whitelist",
                "target": _resolve_semantic_name(semantic_name, alias_map) or semantic_name,
                "passed": bool(field and field.field_name in whitelist.get(field.table_name, set()) and schema_passed),
                "table_name": field.table_name if field else None,
                "field_name": field.field_name if field else None,
            }
        )

    for relation in scene.relations:
        checks.append(
            {
                "type": "relation_whitelist",
                "target": relation.relation_id,
                "passed": (
                    relation.left_field in whitelist.get(relation.left_table, set())
                    and relation.right_field in whitelist.get(relation.right_table, set())
                    and (relation.left_field in db_schema.get(relation.left_table, set()) if db_schema else True)
                    and (relation.right_field in db_schema.get(relation.right_table, set()) if db_schema else True)
                ),
                "left_table": relation.left_table,
                "right_table": relation.right_table,
            }
        )

    checks.append({"type": "read_only", "passed": True})
    checks.append({"type": "row_limit", "passed": True, "limit": "unlimited"})
    return checks


def _quote_identifier(identifier: str) -> str:
    if not IDENTIFIER_RE.match(identifier):
        raise ValueError(f"invalid identifier: {identifier}")
    return f"`{identifier}`"


def _normalized_join_type(join_type: str) -> str:
    normalized = (join_type or "INNER").strip().upper()
    if normalized not in ALLOWED_JOIN_TYPES:
        return "INNER"
    return normalized


def _quote_alias(alias: str) -> str:
    return f"`{alias.replace('`', '``')}`"


def _build_filters(
    queryable_fields,
    alias_map: dict[str, str],
    query_plan: QueryPlanDTO,
    aliases: dict[str, str],
    alias_for,
) -> tuple[list[str], list]:
    where_parts: list[str] = []
    params: list = []

    def is_placeholder_value(raw) -> bool:
        if raw is None:
            return True
        if isinstance(raw, str):
            text = raw.strip()
            return text in {"", "待确认", "待补充", "unknown", "tbd"}
        if isinstance(raw, list):
            normalized = [str(item).strip() for item in raw if str(item).strip()]
            if not normalized:
                return True
            return all(item in {"待确认", "待补充", "unknown", "tbd"} for item in normalized)
        return False

    for condition in query_plan.filters or []:
        semantic_name = condition.get("field")
        if not semantic_name:
            continue
        field = _find_scene_field(queryable_fields, semantic_name, alias_map)
        if not field:
            continue
        operator = str(condition.get("operator", "=")).strip().lower()
        value = condition.get("value")
        if is_placeholder_value(value):
            continue
        table_alias = aliases.get(field.table_name) or alias_for(field.table_name)
        field_sql = f"{table_alias}.{_quote_identifier(field.field_name)}"
        if operator == "in" and isinstance(value, list) and value:
            placeholders = ", ".join(["%s"] * len(value))
            where_parts.append(f"{field_sql} IN ({placeholders})")
            params.extend(value)
        elif operator == "=":
            where_parts.append(f"{field_sql} = %s")
            params.append(value)
        elif operator in {">", ">=", "<", "<="}:
            where_parts.append(f"{field_sql} {operator} %s")
            params.append(value)
        elif operator == "between" and isinstance(value, list) and len(value) == 2:
            where_parts.append(f"{field_sql} BETWEEN %s AND %s")
            params.extend([value[0], value[1]])
        elif operator == "like":
            where_parts.append(f"{field_sql} LIKE %s")
            params.append(value)
    return where_parts, params


def _build_mysql_query(scene: SceneDTO, query_plan: QueryPlanDTO) -> tuple[str, list, list[str], list[str]]:
    queryable_fields, alias_map = _queryable_context(scene)
    dimensions = [_resolve_semantic_name(item, alias_map) for item in (query_plan.dimensions or [])]
    metrics = [item for item in (query_plan.metrics or []) if item == COUNT_SENTINEL] + [
        _resolve_semantic_name(item, alias_map) for item in (query_plan.metrics or []) if item != COUNT_SENTINEL
    ]
    metric_aggregation = _metric_aggregation_from_intent(query_plan.intent or "")
    sort_direction = _intent_sort_direction(_normalize_intent_text(query_plan.intent or ""))
    top_n = _intent_top_n(query_plan.intent or "")
    relations = scene.relations
    filter_fields = [
        field
        for field in (
            _find_scene_field(queryable_fields, condition.get("field"), alias_map)
            for condition in (query_plan.filters or [])
        )
        if field is not None
    ]

    aliases: dict[str, str] = {}
    from_table = None
    select_parts: list[str] = []
    group_parts: list[str] = []
    join_parts: list[str] = []
    joined_tables: set[str] = set()

    def alias_for(table_name: str) -> str:
        if table_name not in aliases:
            aliases[table_name] = f"t{len(aliases)}"
        return aliases[table_name]

    metric_fields_resolved = [_find_scene_field(queryable_fields, semantic_name, alias_map) for semantic_name in metrics if semantic_name != COUNT_SENTINEL]
    metric_fields_resolved = [field for field in metric_fields_resolved if field]
    if metric_fields_resolved:
        from_table = metric_fields_resolved[0].table_name

    for semantic_name in dimensions:
        field = _find_scene_field(queryable_fields, semantic_name, alias_map)
        if not field:
            continue
        if from_table is None:
            from_table = field.table_name
        table_alias = alias_for(field.table_name)
        select_parts.append(
            f"{table_alias}.{_quote_identifier(field.field_name)} AS {_quote_alias(semantic_name)}"
        )
        group_parts.append(f"{table_alias}.{_quote_identifier(field.field_name)}")

    metric_aliases: list[str] = []
    if COUNT_SENTINEL in metrics:
        metric_aliases.append("count_rows")
        select_parts.append("COUNT(*) AS `count_rows`")

    for field in metric_fields_resolved:
        if not field:
            continue
        if from_table is None:
            from_table = field.table_name
        table_alias = alias_for(field.table_name)
        metric_aliases.append(field.semantic_name)
        if metric_aggregation == "sum":
            metric_sql = f"SUM({table_alias}.{_quote_identifier(field.field_name)})"
        elif metric_aggregation == "max":
            metric_sql = f"MAX({table_alias}.{_quote_identifier(field.field_name)})"
        elif metric_aggregation == "min":
            metric_sql = f"MIN({table_alias}.{_quote_identifier(field.field_name)})"
        else:
            metric_sql = f"AVG({table_alias}.{_quote_identifier(field.field_name)})"
        select_parts.append(
            f"{metric_sql} AS {_quote_alias(field.semantic_name)}"
        )

    if from_table is None:
        raise ValueError("no queryable fields found in scene")

    base_alias = alias_for(from_table)
    joined_tables.add(from_table)
    used_tables = {
        field.table_name for field in queryable_fields if field.semantic_name in dimensions + metrics
    } | {field.table_name for field in filter_fields}
    while True:
        progress = False
        for relation in relations:
            if relation.left_table not in used_tables or relation.right_table not in used_tables:
                continue
            if relation.left_table in joined_tables and relation.right_table not in joined_tables:
                left_alias = alias_for(relation.left_table)
                right_alias = alias_for(relation.right_table)
                join_sql = (
                    f" {_normalized_join_type(relation.join_type)} JOIN {_quote_identifier(relation.right_table)} {right_alias}"
                    f" ON {left_alias}.{_quote_identifier(relation.left_field)}"
                    f" = {right_alias}.{_quote_identifier(relation.right_field)}"
                )
                join_parts.append(join_sql)
                joined_tables.add(relation.right_table)
                progress = True
            elif relation.right_table in joined_tables and relation.left_table not in joined_tables:
                right_alias = alias_for(relation.right_table)
                left_alias = alias_for(relation.left_table)
                join_sql = (
                    f" {_normalized_join_type(relation.join_type)} JOIN {_quote_identifier(relation.left_table)} {left_alias}"
                    f" ON {left_alias}.{_quote_identifier(relation.left_field)}"
                    f" = {right_alias}.{_quote_identifier(relation.right_field)}"
                )
                join_parts.append(join_sql)
                joined_tables.add(relation.left_table)
                progress = True
        if not progress:
            break

    where_parts, params = _build_filters(
        queryable_fields=queryable_fields,
        alias_map=alias_map,
        query_plan=query_plan,
        aliases=aliases,
        alias_for=alias_for,
    )
    sql = f"SELECT {', '.join(select_parts)} FROM {_quote_identifier(from_table)} {base_alias}{''.join(join_parts)}"
    if where_parts:
        sql += f" WHERE {' AND '.join(where_parts)}"
    if group_parts:
        sql += f" GROUP BY {', '.join(group_parts)}"
    if top_n > 0:
        if metric_aliases:
            sql += f" ORDER BY {_quote_alias(metric_aliases[0])} {sort_direction}"
        elif dimensions:
            sql += f" ORDER BY {_quote_alias(dimensions[0])} {sort_direction}"
        sql += f" LIMIT {top_n}"
    return sql, params, dimensions, metric_aliases or metrics


def _execute_mysql_query(scene: SceneDTO, scene_version: str | None, query_plan: QueryPlanDTO, session_id: str) -> QueryRunDTO:
    if not (query_plan.metrics or query_plan.dimensions):
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan.query_plan_id,
            sql="",
            sql_explanation="QueryPlan 缺少可执行语义字段，需补模后再执行。",
            status="failed",
            rows_count=0,
            duration_ms=0,
            result_preview=[],
            insight_summary=["需补模：当前场景没有可执行语义字段"],
            chart_suggestion="table",
            safety_checks=[{"type": "semantic_queryable_fields", "passed": False}],
            lineage={"scene_id": scene.scene_id, "scene_version": scene_version, "execution_mode": "mysql_blocked"},
        )
    mysql_config = _mysql_config()
    started_at = time.perf_counter()
    try:
        connection = pymysql.connect(**mysql_config)
    except Exception as exc:  # noqa: BLE001
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan.query_plan_id,
            sql="",
            sql_explanation="MySQL 连接失败，无法执行查询。",
            status="failed",
            rows_count=0,
            duration_ms=0,
            result_preview=[],
            insight_summary=[f"MySQL 连接失败: {exc}"],
            chart_suggestion="table",
            safety_checks=[{"type": "mysql_connection", "passed": False}],
            lineage={"scene_id": scene.scene_id, "scene_version": scene_version, "execution_mode": "mysql"},
        )
    try:
        db_schema = _get_mysql_schema(connection)
    except Exception:
        db_schema = {}
    safety_checks = _validate_query_plan(scene, query_plan, db_schema=db_schema)
    if not all(check["passed"] for check in safety_checks):
        missing_semantic_fields = [
            str(check.get("target") or "").strip()
            for check in safety_checks
            if check.get("type") == "field_whitelist" and not check.get("passed")
        ]
        missing_semantic_fields = [item for item in missing_semantic_fields if item]
        missing_text = "、".join(sorted(set(missing_semantic_fields))) or "字段映射不完整"
        connection.close()
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan.query_plan_id,
            sql="",
            sql_explanation=f"QueryPlan 未通过语义白名单校验：{missing_text}，需补模后再执行。",
            status="failed",
            rows_count=0,
            duration_ms=0,
            result_preview=[],
            insight_summary=[f"需补模：{missing_text}"],
            chart_suggestion="table",
            safety_checks=safety_checks,
            lineage={"scene_id": scene.scene_id, "scene_version": scene_version, "execution_mode": "mysql_blocked"},
        )
    try:
        sql_base, sql_params, dimensions, metrics = _build_mysql_query(scene, query_plan)
    except Exception as exc:
        connection.close()
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan.query_plan_id,
            sql="",
            sql_explanation="SQL 生成失败。",
            status="failed",
            rows_count=0,
            duration_ms=0,
            result_preview=[],
            insight_summary=[f"SQL 生成失败: {exc}"],
            chart_suggestion="table",
            safety_checks=safety_checks,
            lineage={"scene_id": scene.scene_id, "scene_version": scene_version, "execution_mode": "mysql"},
        )
    sql = sql_base

    try:
        with connection.cursor() as cursor:
            cursor.execute(sql, sql_params)
            rows = cursor.fetchall()
    except Exception as exc:  # noqa: BLE001
        connection.close()
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan.query_plan_id,
            sql=sql,
            sql_explanation="MySQL 查询执行失败。",
            status="failed",
            rows_count=0,
            duration_ms=duration_ms,
            result_preview=[],
            insight_summary=[f"MySQL 执行失败: {exc}"],
            chart_suggestion="table",
            safety_checks=safety_checks,
            lineage={
                "scene_id": scene.scene_id,
                "scene_version": scene_version,
                "execution_mode": "mysql",
                "database": mysql_config["database"],
            },
        )
    finally:
        connection.close()

    duration_ms = int((time.perf_counter() - started_at) * 1000)
    result_preview = rows
    if not result_preview:
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan.query_plan_id,
            sql=sql,
            sql_explanation="已执行 MySQL 查询，但结果为空。",
            status="succeeded",
            rows_count=0,
            duration_ms=duration_ms,
            result_preview=[],
            insight_summary=["当前查询未返回数据，请调整筛选条件或语义字段配置"],
            chart_suggestion="table",
            safety_checks=safety_checks,
            lineage={
                "scene_id": scene.scene_id,
                "scene_version": scene_version,
                "execution_mode": "mysql",
                "database": mysql_config["database"],
                "host": mysql_config["host"],
            },
        )

    primary_metric = metrics[0] if metrics else next(iter(result_preview[0].keys()))
    if primary_metric in result_preview[0]:
        top_row = max(result_preview, key=lambda row: float(row.get(primary_metric, 0) or 0))
        bottom_row = min(result_preview, key=lambda row: float(row.get(primary_metric, 0) or 0))
        lead_dimension = dimensions[0] if dimensions else "当前分组"
        insight_summary = [
            f"{top_row.get(lead_dimension, '当前分组')} 在 {primary_metric} 上当前最高，为 {top_row.get(primary_metric)}",
            f"{bottom_row.get(lead_dimension, '当前分组')} 在 {primary_metric} 上当前最低，为 {bottom_row.get(primary_metric)}",
            f"本轮结果来自真实 MySQL：{mysql_config['database']}",
        ]
    else:
        insight_summary = [f"本轮结果来自真实 MySQL：{mysql_config['database']}"]

    return QueryRunDTO(
        query_id=f"query_{uuid4().hex[:10]}",
        session_id=session_id,
        query_plan_id=query_plan.query_plan_id,
        sql=sql,
        sql_explanation="根据场景关系和语义字段，执行真实 MySQL 聚合查询。",
        status="succeeded",
        rows_count=len(rows),
        duration_ms=duration_ms,
        result_preview=result_preview,
        insight_summary=insight_summary,
        chart_suggestion=query_plan.chart_candidates[0] if query_plan.chart_candidates else "bar",
        safety_checks=safety_checks,
        lineage={
            "tables": sorted(
                {
                    field.table_name
                    for field in _queryable_context(scene)[0]
                    if field.semantic_name in dimensions + metrics
                }
            ),
            "scene_id": scene.scene_id,
            "scene_version": scene_version,
            "dimensions": dimensions,
            "metrics": metrics,
            "execution_mode": "mysql",
            "database": mysql_config["database"],
            "host": mysql_config["host"],
            "row_limit": "unlimited",
        },
    )


def build_query_plan(scene: SceneDTO, global_goal: str, session_id: str) -> QueryPlanDTO:
    queryable_fields, _ = _queryable_context(scene)
    metric_fields, dimension_fields = _pick_fields_from_intent(queryable_fields, global_goal)
    time_fields = [field.semantic_name for field in queryable_fields if field.role == "time" and field.enabled]
    filters: list[dict] = []
    risk_notes = [
        "当前执行链路为 MySQL 实库查询",
        f"当前 scene version={scene.version}",
    ]
    if not queryable_fields:
        risk_notes.append("当前无可用语义字段，需补模后再执行")
    return QueryPlanDTO(
        query_plan_id=f"qp_{uuid4().hex[:10]}",
        session_id=session_id,
        intent=global_goal.strip() or f"围绕场景 {scene.name} 进行分析",
        metrics=metric_fields[:2],
        dimensions=dimension_fields[:2],
        filters=filters,
        time_window=f"围绕 {time_fields[0]}" if time_fields else "最近30天",
        chart_candidates=["bar", "line", "table"],
        risk_notes=risk_notes,
    )


def execute_query_plan(
    session_id: str,
    scene: SceneDTO,
    scene_version: str | None,
    query_plan: QueryPlanDTO,
) -> QueryRunDTO:
    return _execute_mysql_query(
        scene=scene,
        scene_version=scene_version,
        query_plan=query_plan,
        session_id=session_id,
    )


def execute_raw_sql(
    *,
    session_id: str,
    scene: SceneDTO,
    scene_version: str | None,
    sql: str,
    query_plan_id: str | None = None,
    sql_explanation: str = "",
    lineage_extra: dict | None = None,
) -> QueryRunDTO:
    sql_text = str(sql or "").strip()
    sql_text, mysql_quotes_normalized = _normalize_mysql_identifier_quotes(sql_text)
    if not sql_text:
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan_id,
            sql="",
            sql_explanation="SQL 为空，无法执行。",
            status="failed",
            rows_count=0,
            duration_ms=0,
            result_preview=[],
            insight_summary=["sql-agent 未返回可执行 SQL"],
            chart_suggestion="table",
            safety_checks=[{"type": "sql_present", "passed": False}],
            lineage={"scene_id": scene.scene_id, "scene_version": scene_version, "execution_mode": "mysql_raw"},
        )

    normalized = sql_text.rstrip(";").strip()
    lower_sql = normalized.lower()
    if ";" in normalized:
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan_id,
            sql=sql_text,
            sql_explanation="只允许单条只读 SQL。",
            status="failed",
            rows_count=0,
            duration_ms=0,
            result_preview=[],
            insight_summary=["检测到多语句 SQL，已拦截"],
            chart_suggestion="table",
            safety_checks=[{"type": "single_statement", "passed": False}],
            lineage={"scene_id": scene.scene_id, "scene_version": scene_version, "execution_mode": "mysql_raw"},
        )
    if not (lower_sql.startswith("select") or lower_sql.startswith("with")):
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan_id,
            sql=sql_text,
            sql_explanation="仅允许 SELECT/CTE 查询。",
            status="failed",
            rows_count=0,
            duration_ms=0,
            result_preview=[],
            insight_summary=["当前仅支持只读查询，写操作已拦截"],
            chart_suggestion="table",
            safety_checks=[{"type": "read_only_sql", "passed": False}],
            lineage={"scene_id": scene.scene_id, "scene_version": scene_version, "execution_mode": "mysql_raw"},
        )
    if BLOCKED_SQL_KEYWORDS.search(lower_sql):
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan_id,
            sql=sql_text,
            sql_explanation="检测到潜在写操作关键字，已拦截。",
            status="failed",
            rows_count=0,
            duration_ms=0,
            result_preview=[],
            insight_summary=["SQL 包含潜在写操作关键字，已拦截"],
            chart_suggestion="table",
            safety_checks=[{"type": "blocked_keywords", "passed": False}],
            lineage={"scene_id": scene.scene_id, "scene_version": scene_version, "execution_mode": "mysql_raw"},
        )
    if UNBOUND_PLACEHOLDER_SQL_RE.search(normalized):
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan_id,
            sql=sql_text,
            sql_explanation="检测到未绑定占位符，已阻止执行。",
            status="failed",
            rows_count=0,
            duration_ms=0,
            result_preview=[],
            insight_summary=["SQL 包含未绑定占位符，不能作为生产查询执行"],
            chart_suggestion="table",
            safety_checks=[{"type": "unbound_placeholder", "passed": False}],
            lineage={"scene_id": scene.scene_id, "scene_version": scene_version, "execution_mode": "mysql_raw"},
        )
    if NON_MYSQL_INTERVAL_LITERAL_RE.search(normalized):
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan_id,
            sql=sql_text,
            sql_explanation="检测到非 MySQL 时间间隔语法，已阻止执行。",
            status="failed",
            rows_count=0,
            duration_ms=0,
            result_preview=[],
            insight_summary=["SQL 使用了非 MySQL 写法 INTERVAL '30 day'，不能执行"],
            chart_suggestion="table",
            safety_checks=[{"type": "mysql_interval_syntax", "passed": False}],
            lineage={"scene_id": scene.scene_id, "scene_version": scene_version, "execution_mode": "mysql_raw"},
        )

    mysql_config = _mysql_config()
    started_at = time.perf_counter()
    try:
        connection = pymysql.connect(**mysql_config)
    except Exception as exc:  # noqa: BLE001
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan_id,
            sql=sql_text,
            sql_explanation="MySQL 连接失败，无法执行查询。",
            status="failed",
            rows_count=0,
            duration_ms=0,
            result_preview=[],
            insight_summary=[f"MySQL 连接失败: {exc}"],
            chart_suggestion="table",
            safety_checks=[{"type": "mysql_connection", "passed": False}],
            lineage={"scene_id": scene.scene_id, "scene_version": scene_version, "execution_mode": "mysql_raw"},
        )

    try:
        with connection.cursor() as cursor:
            cursor.execute(sql_text)
            rows = cursor.fetchall()
    except Exception as exc:  # noqa: BLE001
        duration_ms = int((time.perf_counter() - started_at) * 1000)
        return QueryRunDTO(
            query_id=f"query_{uuid4().hex[:10]}",
            session_id=session_id,
            query_plan_id=query_plan_id,
            sql=sql_text,
            sql_explanation="MySQL 查询执行失败。",
            status="failed",
            rows_count=0,
            duration_ms=duration_ms,
            result_preview=[],
            insight_summary=[f"MySQL 执行失败: {exc}"],
            chart_suggestion="table",
            safety_checks=[{"type": "mysql_execution", "passed": False}],
            lineage={
                "scene_id": scene.scene_id,
                "scene_version": scene_version,
                "execution_mode": "mysql_raw",
                "database": mysql_config["database"],
            },
        )
    finally:
        connection.close()

    duration_ms = int((time.perf_counter() - started_at) * 1000)
    lineage = {
        "scene_id": scene.scene_id,
        "scene_version": scene_version,
        "execution_mode": "mysql_raw",
        "database": mysql_config["database"],
        "host": mysql_config["host"],
    }
    if isinstance(lineage_extra, dict):
        lineage.update(lineage_extra)
    return QueryRunDTO(
        query_id=f"query_{uuid4().hex[:10]}",
        session_id=session_id,
        query_plan_id=query_plan_id,
        sql=sql_text,
        sql_explanation=sql_explanation or "SQL Agent 直接生成 SQL 并执行。",
        status="succeeded",
        rows_count=len(rows),
        duration_ms=duration_ms,
        result_preview=rows,
        insight_summary=[f"本轮结果来自 SQL Agent 直连 MySQL：{mysql_config['database']}"],
        chart_suggestion="table",
        safety_checks=[
            {"type": "read_only_sql", "passed": True},
            {"type": "single_statement", "passed": True},
            {"type": "mysql_identifier_quotes", "passed": True, "normalized": mysql_quotes_normalized},
        ],
        lineage=lineage,
    )
