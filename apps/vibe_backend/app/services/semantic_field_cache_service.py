from __future__ import annotations

import hashlib
import json
import os
import threading
from uuid import uuid4

import pymysql

from packages.shared_contracts.python_models import FieldRole, SceneFieldDTO


SEMANTIC_ZONES = {"modeled", "effective"}
FIELD_ROLES = {role.value for role in FieldRole}


def _mysql_config() -> dict:
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", "root"),
        "database": os.getenv("MYSQL_DATABASE", "dataservice_test_local"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": False,
    }


class SemanticFieldCacheService:
    _lock = threading.Lock()
    _tables_ready = False

    def _connect(self):
        return pymysql.connect(**_mysql_config())

    @staticmethod
    def _build_source_key(semantic_name: str, table_name: str, field_name: str) -> str:
        raw = "|".join([semantic_name.strip().lower(), table_name.strip().lower(), field_name.strip().lower()])
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    def _ensure_tables(self, conn) -> None:
        if self._tables_ready:
            return
        with self._lock:
            if self._tables_ready:
                return
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vibe_semantic_field_cache (
                      cache_id VARCHAR(64) PRIMARY KEY,
                      scene_id VARCHAR(64) NOT NULL,
                      zone VARCHAR(16) NOT NULL DEFAULT 'modeled',
                      semantic_name VARCHAR(255) NOT NULL,
                      semantic_definition TEXT NOT NULL,
                      aliases_json TEXT NOT NULL,
                      unit VARCHAR(64) NOT NULL DEFAULT '',
                      aggregation VARCHAR(64) NOT NULL DEFAULT '',
                      table_name VARCHAR(255) NOT NULL,
                      field_name VARCHAR(255) NOT NULL,
                      source_key CHAR(32) NOT NULL DEFAULT '',
                      er_path TEXT NOT NULL,
                      role VARCHAR(32) NOT NULL DEFAULT 'dimension',
                      enabled TINYINT(1) NOT NULL DEFAULT 1,
                      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                      INDEX idx_semantic_scene_zone_enabled (scene_id, zone, enabled),
                      INDEX idx_semantic_scene_semantic (scene_id, semantic_name),
                      UNIQUE KEY uq_semantic_scene_zone_source (scene_id, zone, source_key)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute("SHOW COLUMNS FROM vibe_semantic_field_cache LIKE 'source_key'")
                has_source_key = bool(cur.fetchone())
                if not has_source_key:
                    cur.execute(
                        """
                        ALTER TABLE vibe_semantic_field_cache
                        ADD COLUMN source_key CHAR(32) NOT NULL DEFAULT '' AFTER field_name
                        """
                    )
                    cur.execute(
                        """
                        UPDATE vibe_semantic_field_cache
                        SET source_key = MD5(CONCAT(LOWER(TRIM(semantic_name)), '|', LOWER(TRIM(table_name)), '|', LOWER(TRIM(field_name))))
                        WHERE source_key = ''
                        """
                    )
                cur.execute("SHOW INDEX FROM vibe_semantic_field_cache WHERE Key_name = 'uq_semantic_scene_zone_source'")
                unique_rows = cur.fetchall() or []
                unique_cols = {
                    str(item.get("Column_name") or "").strip().lower()
                    for item in unique_rows
                    if str(item.get("Non_unique") or "1") == "0"
                }
                if unique_rows and unique_cols != {"scene_id", "zone", "source_key"}:
                    cur.execute("ALTER TABLE vibe_semantic_field_cache DROP INDEX uq_semantic_scene_zone_source")
                    cur.execute(
                        """
                        ALTER TABLE vibe_semantic_field_cache
                        ADD UNIQUE KEY uq_semantic_scene_zone_source (scene_id, zone, source_key)
                        """
                    )
            conn.commit()
            self._tables_ready = True

    @staticmethod
    def _normalize_zone(zone: str | None) -> str:
        normalized = str(zone or "modeled").strip().lower()
        if normalized not in SEMANTIC_ZONES:
            return "modeled"
        return normalized

    @staticmethod
    def _normalize_role(role: str | None) -> str:
        normalized = str(role or FieldRole.DIMENSION.value).strip().lower()
        if normalized not in FIELD_ROLES:
            return FieldRole.DIMENSION.value
        return normalized

    @staticmethod
    def _normalize_aliases(aliases: list[str] | None) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in aliases or []:
            value = str(item).strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(value)
        return cleaned

    @staticmethod
    def _decode_aliases(raw: str | None) -> list[str]:
        text = str(raw or "").strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(parsed, list):
            return []
        return [str(item).strip() for item in parsed if str(item).strip()]

    def list_scene_fields(self, scene_id: str, *, zone: str = "all", include_disabled: bool = False) -> list[dict]:
        scene_key = str(scene_id or "").strip()
        if not scene_key:
            return []
        zone_key = str(zone or "all").strip().lower()
        sql_parts = [
            """
            SELECT
              cache_id, scene_id, zone, semantic_name, semantic_definition,
              aliases_json, unit, aggregation, table_name, field_name, er_path,
              role, enabled, created_at, updated_at
            FROM vibe_semantic_field_cache
            WHERE scene_id = %s
            """,
        ]
        params: list[object] = [scene_key]
        if zone_key in SEMANTIC_ZONES:
            sql_parts.append("AND zone = %s")
            params.append(zone_key)
        if not include_disabled:
            sql_parts.append("AND enabled = 1")
        sql_parts.append("ORDER BY updated_at DESC, created_at DESC, semantic_name, table_name, field_name")
        query = "\n".join(sql_parts)
        try:
            conn = self._connect()
            try:
                self._ensure_tables(conn)
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    rows = cur.fetchall() or []
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return []

        result: list[dict] = []
        for row in rows:
            result.append(
                {
                    "cache_id": str(row.get("cache_id") or "").strip(),
                    "scene_id": scene_key,
                    "zone": self._normalize_zone(str(row.get("zone") or "modeled")),
                    "semantic_name": str(row.get("semantic_name") or "").strip(),
                    "semantic_definition": str(row.get("semantic_definition") or "").strip(),
                    "aliases": self._decode_aliases(row.get("aliases_json")),
                    "unit": str(row.get("unit") or "").strip(),
                    "aggregation": str(row.get("aggregation") or "").strip(),
                    "table_name": str(row.get("table_name") or "").strip(),
                    "field_name": str(row.get("field_name") or "").strip(),
                    "er_path": str(row.get("er_path") or "").strip(),
                    "role": self._normalize_role(str(row.get("role") or FieldRole.DIMENSION.value)),
                    "enabled": bool(row.get("enabled", 1)),
                    "created_at": str(row.get("created_at") or ""),
                    "updated_at": str(row.get("updated_at") or ""),
                }
            )
        if zone_key == "all":
            deduped: dict[tuple[str, str, str], dict] = {}
            ordered_keys: list[tuple[str, str, str]] = []
            for row in result:
                key = (
                    str(row.get("semantic_name") or "").strip().lower(),
                    str(row.get("table_name") or "").strip().lower(),
                    str(row.get("field_name") or "").strip().lower(),
                )
                if not all(key):
                    continue
                existing = deduped.get(key)
                if existing is None:
                    deduped[key] = row
                    ordered_keys.append(key)
                    continue
                current_score = (
                    1 if row.get("zone") == "modeled" else 0,
                    1 if row.get("enabled") else 0,
                    str(row.get("updated_at") or row.get("created_at") or ""),
                )
                existing_score = (
                    1 if existing.get("zone") == "modeled" else 0,
                    1 if existing.get("enabled") else 0,
                    str(existing.get("updated_at") or existing.get("created_at") or ""),
                )
                if current_score > existing_score:
                    deduped[key] = row
            result = [deduped[key] for key in ordered_keys]
        return result

    def upsert_field(self, scene_id: str, payload: dict, *, cache_id: str | None = None) -> dict:
        scene_key = str(scene_id or "").strip()
        if not scene_key:
            raise ValueError("scene_id is required")

        semantic_name = str(payload.get("semantic_name") or "").strip()
        table_name = str(payload.get("table_name") or "").strip()
        field_name = str(payload.get("field_name") or "").strip()
        if not semantic_name or not table_name or not field_name:
            raise ValueError("semantic_name/table_name/field_name are required")
        source_key = self._build_source_key(semantic_name, table_name, field_name)

        provided_cache_key = str(cache_id or payload.get("cache_id") or "").strip()
        cache_key = provided_cache_key or f"sem_{uuid4().hex[:12]}"
        zone = self._normalize_zone(payload.get("zone"))
        role = self._normalize_role(payload.get("role"))
        aliases = self._normalize_aliases(payload.get("aliases") if isinstance(payload.get("aliases"), list) else [])

        semantic_definition = str(payload.get("semantic_definition") or "").strip()
        unit = str(payload.get("unit") or "").strip()
        aggregation = str(payload.get("aggregation") or "").strip()
        er_path = str(payload.get("er_path") or "").strip()
        enabled = bool(payload.get("enabled", True))

        conn = self._connect()
        try:
            self._ensure_tables(conn)
            with conn.cursor() as cur:
                if not provided_cache_key:
                    cur.execute(
                        """
                        SELECT cache_id
                        FROM vibe_semantic_field_cache
                        WHERE scene_id = %s AND zone = %s AND LOWER(TRIM(table_name)) = %s AND LOWER(TRIM(field_name)) = %s
                        ORDER BY updated_at DESC, created_at DESC
                        LIMIT 1
                        """,
                        (scene_key, zone, table_name.lower(), field_name.lower()),
                    )
                    existing = cur.fetchone() or {}
                    existing_cache_id = str(existing.get("cache_id") or "").strip()
                    if existing_cache_id:
                        cache_key = existing_cache_id
                cur.execute(
                    """
                    INSERT INTO vibe_semantic_field_cache (
                      cache_id, scene_id, zone, semantic_name, semantic_definition,
                      aliases_json, unit, aggregation, table_name, field_name, source_key, er_path,
                      role, enabled
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      scene_id = VALUES(scene_id),
                      zone = VALUES(zone),
                      semantic_name = VALUES(semantic_name),
                      semantic_definition = VALUES(semantic_definition),
                      aliases_json = VALUES(aliases_json),
                      unit = VALUES(unit),
                      aggregation = VALUES(aggregation),
                      table_name = VALUES(table_name),
                      field_name = VALUES(field_name),
                      source_key = VALUES(source_key),
                      er_path = VALUES(er_path),
                      role = VALUES(role),
                      enabled = VALUES(enabled)
                    """,
                    (
                        cache_key,
                        scene_key,
                        zone,
                        semantic_name,
                        semantic_definition,
                        json.dumps(aliases, ensure_ascii=False),
                        unit,
                        aggregation,
                        table_name,
                        field_name,
                        source_key,
                        er_path,
                        role,
                        1 if enabled else 0,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        rows = self.list_scene_fields(scene_key, zone="all", include_disabled=True)
        for row in rows:
            if row.get("cache_id") == cache_key:
                return row
        raise RuntimeError("failed to load upserted semantic cache row")

    def delete_field(self, scene_id: str, cache_id: str) -> bool:
        scene_key = str(scene_id or "").strip()
        cache_key = str(cache_id or "").strip()
        if not scene_key or not cache_key:
            return False
        conn = self._connect()
        try:
            self._ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM vibe_semantic_field_cache WHERE scene_id = %s AND cache_id = %s",
                    (scene_key, cache_key),
                )
                affected = cur.rowcount or 0
            conn.commit()
        finally:
            conn.close()
        return affected > 0

    def delete_scene_fields_by_zone(self, scene_id: str, zone: str) -> int:
        scene_key = str(scene_id or "").strip()
        zone_key = self._normalize_zone(zone)
        if not scene_key:
            return 0
        conn = self._connect()
        try:
            self._ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM vibe_semantic_field_cache WHERE scene_id = %s AND zone = %s",
                    (scene_key, zone_key),
                )
                affected = int(cur.rowcount or 0)
            conn.commit()
        finally:
            conn.close()
        return affected

    def upsert_effective_fields_from_candidates(
        self,
        scene_id: str,
        selected_fields: list[dict],
        *,
        replace_existing_effective: bool = False,
    ) -> dict:
        scene_key = str(scene_id or "").strip()
        if not scene_key:
            return {"inserted_or_updated": 0, "removed_effective": 0}

        removed_effective = 0
        if replace_existing_effective:
            removed_effective = self.delete_scene_fields_by_zone(scene_key, "effective")
            removed_effective += self.delete_scene_fields_by_zone(scene_key, "modeled")

        upserted = 0
        for item in selected_fields or []:
            if not isinstance(item, dict):
                continue
            semantic_name = str(item.get("semantic_name") or "").strip()
            table_name = str(item.get("table_name") or "").strip()
            field_name = str(item.get("field_name") or "").strip()
            if not semantic_name or not table_name or not field_name:
                continue
            description = str(item.get("description") or "").strip()
            if not description:
                description = str(item.get("reason") or "").strip()
            role = str(item.get("role") or FieldRole.DIMENSION.value).strip().lower()
            self.upsert_field(
                scene_key,
                {
                    "semantic_name": semantic_name,
                    "semantic_definition": description,
                    "aliases": [],
                    "unit": "",
                    "aggregation": "",
                    "table_name": table_name,
                    "field_name": field_name,
                    "er_path": "",
                    "role": role,
                    "zone": "modeled",
                    "enabled": bool(item.get("enabled", True)),
                },
            )
            upserted += 1

        return {
            "inserted_or_updated": upserted,
            "removed_effective": removed_effective,
        }

    def get_queryable_alias_map(self, scene_id: str) -> dict[str, str]:
        rows = self.list_scene_fields(scene_id, zone="all", include_disabled=False)
        alias_map: dict[str, str] = {}
        for row in rows:
            semantic_name = str(row.get("semantic_name") or "").strip()
            if not semantic_name:
                continue
            alias_map[semantic_name.lower()] = semantic_name
            for alias in row.get("aliases") or []:
                alias_map[str(alias).strip().lower()] = semantic_name
        return alias_map

    def get_queryable_scene_fields(self, scene_id: str) -> list[SceneFieldDTO]:
        rows = self.list_scene_fields(scene_id, zone="all", include_disabled=False)
        result: list[SceneFieldDTO] = []
        seen_keys: set[tuple[str, str, str]] = set()
        for row in rows:
            semantic_name = str(row.get("semantic_name") or "").strip()
            table_name = str(row.get("table_name") or "").strip()
            field_name = str(row.get("field_name") or "").strip()
            if not semantic_name or not table_name or not field_name:
                continue
            dedupe_key = (semantic_name.lower(), table_name.lower(), field_name.lower())
            if dedupe_key in seen_keys:
                continue
            seen_keys.add(dedupe_key)
            description_parts = [str(row.get("semantic_definition") or "").strip()]
            unit = str(row.get("unit") or "").strip()
            aggregation = str(row.get("aggregation") or "").strip()
            if unit:
                description_parts.append(f"单位={unit}")
            if aggregation:
                description_parts.append(f"聚合={aggregation}")
            description = "；".join([part for part in description_parts if part])
            result.append(
                SceneFieldDTO(
                    field_id=str(row.get("cache_id") or f"sem_{uuid4().hex[:10]}"),
                    table_name=table_name,
                    field_name=field_name,
                    semantic_name=semantic_name,
                    description=description,
                    role=self._normalize_role(str(row.get("role") or FieldRole.DIMENSION.value)),
                    enabled=bool(row.get("enabled", 1)),
                )
            )
        return result

    def seed_modeled_from_scene_fields(self, scene_id: str, scene_fields: list[SceneFieldDTO]) -> int:
        scene_key = str(scene_id or "").strip()
        if not scene_key:
            return 0

        rows = self.list_scene_fields(scene_key, zone="all", include_disabled=True)
        if rows:
            return 0

        seeded = 0
        for field in scene_fields or []:
            semantic_name = str(getattr(field, "semantic_name", "") or "").strip()
            table_name = str(getattr(field, "table_name", "") or "").strip()
            field_name = str(getattr(field, "field_name", "") or "").strip()
            if not semantic_name or not table_name or not field_name:
                continue
            raw_key = f"{scene_key}|modeled|{semantic_name}|{table_name}|{field_name}"
            cache_key = f"sem_{hashlib.md5(raw_key.encode('utf-8')).hexdigest()[:20]}"
            self.upsert_field(
                scene_key,
                {
                    "semantic_name": semantic_name,
                    "semantic_definition": str(getattr(field, "description", "") or "").strip(),
                    "aliases": [],
                    "unit": "",
                    "aggregation": "",
                    "table_name": table_name,
                    "field_name": field_name,
                    "er_path": "",
                    "role": str(getattr(field, "role", FieldRole.DIMENSION.value) or FieldRole.DIMENSION.value),
                    "zone": "modeled",
                    "enabled": bool(getattr(field, "enabled", True)),
                },
                cache_id=cache_key,
            )
            seeded += 1
        return seeded


semantic_field_cache_service = SemanticFieldCacheService()
