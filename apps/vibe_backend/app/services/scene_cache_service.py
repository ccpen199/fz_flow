from __future__ import annotations

import json
import os
import re
import threading
import time

import pymysql

from ..store import SCENES
from packages.shared_contracts.python_models import SceneDTO


_SCENE_ID_PATTERN = re.compile(r"^scene_(\d{4,})$")


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


class SceneCacheService:
    _lock = threading.Lock()
    _tables_ready = False
    _cache_meta: dict[str, object] = {
        "fetched_at": 0.0,
        "last_refresh_at": 0.0,
        "last_refresh_error": None,
        "last_write_at": 0.0,
        "last_write_error": None,
    }

    def __init__(self) -> None:
        self.cache_ttl_seconds = max(0, int(os.getenv("SCENE_CACHE_TTL_SECONDS", "300")))

    def _connect(self):
        return pymysql.connect(**_mysql_config())

    def _ensure_tables(self, conn) -> None:
        if self._tables_ready:
            return
        with self._lock:
            if self._tables_ready:
                return
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vibe_scene (
                      scene_id VARCHAR(64) PRIMARY KEY,
                      name VARCHAR(255) NOT NULL,
                      description TEXT NOT NULL,
                      version INT NOT NULL DEFAULT 1,
                      sample_goals_json LONGTEXT NOT NULL,
                      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vibe_scene_field (
                      field_id VARCHAR(64) PRIMARY KEY,
                      scene_id VARCHAR(64) NOT NULL,
                      table_name VARCHAR(255) NOT NULL,
                      field_name VARCHAR(255) NOT NULL,
                      semantic_name VARCHAR(255) NOT NULL,
                      description TEXT NOT NULL,
                      role VARCHAR(32) NOT NULL,
                      enabled TINYINT(1) NOT NULL DEFAULT 1,
                      display_order INT NOT NULL DEFAULT 0,
                      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                      INDEX idx_scene_field_scene_order (scene_id, display_order),
                      CONSTRAINT fk_scene_field_scene
                        FOREIGN KEY (scene_id) REFERENCES vibe_scene(scene_id)
                        ON DELETE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vibe_scene_relation (
                      relation_id VARCHAR(64) PRIMARY KEY,
                      scene_id VARCHAR(64) NOT NULL,
                      left_table VARCHAR(255) NOT NULL,
                      left_field VARCHAR(255) NOT NULL,
                      right_table VARCHAR(255) NOT NULL,
                      right_field VARCHAR(255) NOT NULL,
                      join_type VARCHAR(32) NOT NULL,
                      note TEXT NOT NULL,
                      display_order INT NOT NULL DEFAULT 0,
                      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                      INDEX idx_scene_relation_scene_order (scene_id, display_order),
                      CONSTRAINT fk_scene_relation_scene
                        FOREIGN KEY (scene_id) REFERENCES vibe_scene(scene_id)
                        ON DELETE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
            conn.commit()
            self._tables_ready = True

    @staticmethod
    def _parse_sample_goals(value: str | None) -> list[str]:
        raw = (value or "").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(data, list):
            return []
        return [str(item).strip() for item in data if str(item).strip()]

    def _fetch_scenes_from_db(self) -> tuple[dict[str, SceneDTO], str | None]:
        try:
            conn = self._connect()
            try:
                self._ensure_tables(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT scene_id, name, description, version, sample_goals_json
                        FROM vibe_scene
                        ORDER BY scene_id
                        """
                    )
                    scene_rows = cur.fetchall() or []

                    cur.execute(
                        """
                        SELECT
                          field_id, scene_id, table_name, field_name, semantic_name,
                          description, role, enabled, display_order
                        FROM vibe_scene_field
                        ORDER BY scene_id, display_order, field_id
                        """
                    )
                    field_rows = cur.fetchall() or []

                    cur.execute(
                        """
                        SELECT
                          relation_id, scene_id, left_table, left_field, right_table,
                          right_field, join_type, note, display_order
                        FROM vibe_scene_relation
                        ORDER BY scene_id, display_order, relation_id
                        """
                    )
                    relation_rows = cur.fetchall() or []
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return {}, str(exc)

        fields_map: dict[str, list[dict]] = {}
        for row in field_rows:
            scene_id = str(row.get("scene_id", "")).strip()
            if not scene_id:
                continue
            fields_map.setdefault(scene_id, []).append(
                {
                    "field_id": row.get("field_id"),
                    "table_name": row.get("table_name"),
                    "field_name": row.get("field_name"),
                    "semantic_name": row.get("semantic_name"),
                    "description": row.get("description") or "",
                    "role": row.get("role") or "dimension",
                    "enabled": bool(row.get("enabled", 1)),
                }
            )

        relations_map: dict[str, list[dict]] = {}
        for row in relation_rows:
            scene_id = str(row.get("scene_id", "")).strip()
            if not scene_id:
                continue
            relations_map.setdefault(scene_id, []).append(
                {
                    "relation_id": row.get("relation_id"),
                    "left_table": row.get("left_table"),
                    "left_field": row.get("left_field"),
                    "right_table": row.get("right_table"),
                    "right_field": row.get("right_field"),
                    "join_type": row.get("join_type") or "INNER",
                    "note": row.get("note") or "",
                }
            )

        scenes: dict[str, SceneDTO] = {}
        for row in scene_rows:
            scene_id = str(row.get("scene_id", "")).strip()
            if not scene_id:
                continue
            payload = {
                "scene_id": scene_id,
                "name": row.get("name") or scene_id,
                "description": row.get("description") or "",
                "version": int(row.get("version") or 1),
                "sample_goals": self._parse_sample_goals(row.get("sample_goals_json")),
                "fields": fields_map.get(scene_id, []),
                "relations": relations_map.get(scene_id, []),
            }
            try:
                scenes[scene_id] = SceneDTO.model_validate(payload)
            except Exception:  # noqa: BLE001
                continue

        return scenes, None

    def _load_cache(self, *, force_refresh: bool = False) -> dict:
        now = time.time()
        with self._lock:
            fetched_at = float(self._cache_meta.get("fetched_at", 0.0) or 0.0)
            has_payload = bool(SCENES)
            cache_age_seconds = max(0, int(now - fetched_at)) if fetched_at else None
            if (
                not force_refresh
                and has_payload
                and fetched_at
                and (now - fetched_at) <= self.cache_ttl_seconds
            ):
                return {
                    "cache_hit": True,
                    "cache_age_seconds": cache_age_seconds,
                    "fetched_at": fetched_at,
                    "ttl_seconds": self.cache_ttl_seconds,
                    "scene_count": len(SCENES),
                    "last_refresh_at": self._cache_meta.get("last_refresh_at"),
                    "last_refresh_error": self._cache_meta.get("last_refresh_error"),
                    "last_write_at": self._cache_meta.get("last_write_at"),
                    "last_write_error": self._cache_meta.get("last_write_error"),
                }

        scenes, refresh_error = self._fetch_scenes_from_db()
        refreshed_at = time.time()

        with self._lock:
            if not refresh_error:
                SCENES.clear()
                SCENES.update(scenes)
                self._cache_meta["fetched_at"] = refreshed_at
            elif not SCENES:
                SCENES.clear()
                SCENES.update(scenes)
                self._cache_meta["fetched_at"] = refreshed_at

            self._cache_meta["last_refresh_at"] = refreshed_at
            self._cache_meta["last_refresh_error"] = refresh_error

            fetched_at = float(self._cache_meta.get("fetched_at", 0.0) or 0.0)
            cache_age_seconds = max(0, int(refreshed_at - fetched_at)) if fetched_at else None
            return {
                "cache_hit": False,
                "cache_age_seconds": cache_age_seconds,
                "fetched_at": fetched_at,
                "ttl_seconds": self.cache_ttl_seconds,
                "scene_count": len(SCENES),
                "last_refresh_at": refreshed_at,
                "last_refresh_error": refresh_error,
                "last_write_at": self._cache_meta.get("last_write_at"),
                "last_write_error": self._cache_meta.get("last_write_error"),
            }

    def ensure_loaded(self) -> dict:
        return self._load_cache(force_refresh=False)

    def refresh_cache(self) -> dict:
        meta = self._load_cache(force_refresh=True)
        return {
            "ok": not bool(meta.get("last_refresh_error")),
            "scene_count": meta.get("scene_count", 0),
            "cache_hit": meta.get("cache_hit", False),
            "cache_ttl_seconds": meta.get("ttl_seconds", self.cache_ttl_seconds),
            "cache_age_seconds": meta.get("cache_age_seconds"),
            "fetched_at": meta.get("fetched_at"),
            "last_refresh_at": meta.get("last_refresh_at"),
            "last_refresh_error": meta.get("last_refresh_error"),
            "last_write_at": meta.get("last_write_at"),
            "last_write_error": meta.get("last_write_error"),
        }

    def cache_status(self) -> dict:
        meta = self._load_cache(force_refresh=False)
        return {
            "ok": True,
            "scene_count": meta.get("scene_count", 0),
            "cache_hit": meta.get("cache_hit", False),
            "cache_ttl_seconds": meta.get("ttl_seconds", self.cache_ttl_seconds),
            "cache_age_seconds": meta.get("cache_age_seconds"),
            "fetched_at": meta.get("fetched_at"),
            "last_refresh_at": meta.get("last_refresh_at"),
            "last_refresh_error": meta.get("last_refresh_error"),
            "last_write_at": meta.get("last_write_at"),
            "last_write_error": meta.get("last_write_error"),
        }

    def list_scenes(self) -> list[SceneDTO]:
        self.ensure_loaded()
        return list(SCENES.values())

    def get_scene(self, scene_id: str) -> SceneDTO | None:
        self.ensure_loaded()
        return SCENES.get(scene_id)

    def upsert_scene(self, scene: SceneDTO) -> SceneDTO:
        now = time.time()
        error: str | None = None
        try:
            conn = self._connect()
            try:
                try:
                    self._ensure_tables(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO vibe_scene (scene_id, name, description, version, sample_goals_json)
                            VALUES (%s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE
                              name = VALUES(name),
                              description = VALUES(description),
                              version = VALUES(version),
                              sample_goals_json = VALUES(sample_goals_json)
                            """,
                            (
                                scene.scene_id,
                                scene.name,
                                scene.description,
                                int(scene.version),
                                json.dumps(scene.sample_goals or [], ensure_ascii=False),
                            ),
                        )

                        cur.execute("DELETE FROM vibe_scene_field WHERE scene_id = %s", (scene.scene_id,))
                        if scene.fields:
                            cur.executemany(
                                """
                                INSERT INTO vibe_scene_field (
                                  field_id, scene_id, table_name, field_name, semantic_name,
                                  description, role, enabled, display_order
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                [
                                    (
                                        field.field_id,
                                        scene.scene_id,
                                        field.table_name,
                                        field.field_name,
                                        field.semantic_name,
                                        field.description,
                                        field.role.value,
                                        1 if field.enabled else 0,
                                        index,
                                    )
                                    for index, field in enumerate(scene.fields)
                                ],
                            )

                        cur.execute("DELETE FROM vibe_scene_relation WHERE scene_id = %s", (scene.scene_id,))
                        if scene.relations:
                            cur.executemany(
                                """
                                INSERT INTO vibe_scene_relation (
                                  relation_id, scene_id, left_table, left_field,
                                  right_table, right_field, join_type, note, display_order
                                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                                """,
                                [
                                    (
                                        relation.relation_id,
                                        scene.scene_id,
                                        relation.left_table,
                                        relation.left_field,
                                        relation.right_table,
                                        relation.right_field,
                                        relation.join_type,
                                        relation.note,
                                        index,
                                    )
                                    for index, relation in enumerate(scene.relations)
                                ],
                            )
                    conn.commit()
                except Exception:  # noqa: BLE001
                    conn.rollback()
                    raise
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            with self._lock:
                self._cache_meta["last_write_at"] = now
                self._cache_meta["last_write_error"] = error
            raise

        with self._lock:
            SCENES[scene.scene_id] = scene
            self._cache_meta["fetched_at"] = now
            self._cache_meta["last_write_at"] = now
            self._cache_meta["last_write_error"] = None
        return scene

    def delete_scene(self, scene_id: str) -> None:
        now = time.time()
        error: str | None = None
        try:
            conn = self._connect()
            try:
                try:
                    self._ensure_tables(conn)
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM vibe_scene WHERE scene_id = %s", (scene_id,))
                    conn.commit()
                except Exception:  # noqa: BLE001
                    conn.rollback()
                    raise
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            with self._lock:
                self._cache_meta["last_write_at"] = now
                self._cache_meta["last_write_error"] = error
            raise

        with self._lock:
            SCENES.pop(scene_id, None)
            self._cache_meta["fetched_at"] = now
            self._cache_meta["last_write_at"] = now
            self._cache_meta["last_write_error"] = None

    def next_custom_scene_id(self) -> str:
        self.ensure_loaded()
        max_no = 0
        for scene_id in SCENES.keys():
            matched = _SCENE_ID_PATTERN.match(scene_id)
            if not matched:
                continue
            max_no = max(max_no, int(matched.group(1)))
        return f"scene_{max_no + 1:04d}"


scene_cache_service = SceneCacheService()
