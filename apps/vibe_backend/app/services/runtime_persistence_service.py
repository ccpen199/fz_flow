from __future__ import annotations

import json
import os
import threading
from typing import Any

import pymysql

from packages.shared_contracts.python_models import QueryPlanDTO, QueryRunDTO


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


class RuntimePersistenceService:
    _lock = threading.Lock()
    _tables_ready = False

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
                    CREATE TABLE IF NOT EXISTS vibe_llm_recommendation_state (
                      scene_id VARCHAR(64) PRIMARY KEY,
                      recommendation_id VARCHAR(64) NOT NULL DEFAULT '',
                      recommendation_json LONGTEXT NOT NULL,
                      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vibe_query_run_state (
                      query_id VARCHAR(64) PRIMARY KEY,
                      session_id VARCHAR(64) NOT NULL,
                      query_plan_id VARCHAR(64) NOT NULL DEFAULT '',
                      query_plan_json LONGTEXT NOT NULL,
                      query_run_json LONGTEXT NOT NULL,
                      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                      INDEX idx_query_state_session_updated (session_id, updated_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vibe_workflow_state (
                      state_key VARCHAR(64) PRIMARY KEY,
                      state_json LONGTEXT NOT NULL,
                      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS vibe_workflow_stage_state (
                      session_id VARCHAR(64) NOT NULL,
                      stage_key VARCHAR(64) NOT NULL,
                      stage_json LONGTEXT NOT NULL,
                      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                      PRIMARY KEY (session_id, stage_key),
                      INDEX idx_workflow_stage_updated (updated_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
            conn.commit()
            self._tables_ready = True

    @staticmethod
    def _json_dumps(payload: Any) -> str:
        return json.dumps(payload or {}, ensure_ascii=False, default=str)

    @staticmethod
    def _json_loads(raw: str | None) -> dict:
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except Exception:  # noqa: BLE001
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def save_recommendation(self, scene_id: str, recommendation: dict) -> dict:
        scene_key = str(scene_id or "").strip()
        if not scene_key:
            raise ValueError("scene_id is required")
        recommendation_id = str((recommendation or {}).get("recommendation_id") or "").strip()
        conn = self._connect()
        try:
            self._ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO vibe_llm_recommendation_state (
                      scene_id, recommendation_id, recommendation_json
                    ) VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      recommendation_id = VALUES(recommendation_id),
                      recommendation_json = VALUES(recommendation_json)
                    """,
                    (scene_key, recommendation_id, self._json_dumps(recommendation)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"ok": True, "scene_id": scene_key, "recommendation_id": recommendation_id}

    def get_recommendation(self, scene_id: str) -> dict | None:
        scene_key = str(scene_id or "").strip()
        if not scene_key:
            return None
        try:
            conn = self._connect()
            try:
                self._ensure_tables(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT recommendation_json
                        FROM vibe_llm_recommendation_state
                        WHERE scene_id = %s
                        """,
                        (scene_key,),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return None
        if not row:
            return None
        recommendation = self._json_loads(row.get("recommendation_json"))
        return recommendation or None

    def save_query_result(
        self,
        *,
        session_id: str,
        query_plan: QueryPlanDTO | None,
        query_run: QueryRunDTO | None,
    ) -> dict:
        if query_run is None:
            return {"ok": False, "reason": "query_run is empty"}
        session_key = str(session_id or query_run.session_id or "").strip()
        if not session_key:
            raise ValueError("session_id is required")
        plan_payload = query_plan.model_dump(mode="json") if query_plan is not None else {}
        run_payload = query_run.model_dump(mode="json")
        conn = self._connect()
        try:
            self._ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO vibe_query_run_state (
                      query_id, session_id, query_plan_id, query_plan_json, query_run_json
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                      session_id = VALUES(session_id),
                      query_plan_id = VALUES(query_plan_id),
                      query_plan_json = VALUES(query_plan_json),
                      query_run_json = VALUES(query_run_json)
                    """,
                    (
                        query_run.query_id,
                        session_key,
                        query_plan.query_plan_id if query_plan is not None else (query_run.query_plan_id or ""),
                        self._json_dumps(plan_payload),
                        self._json_dumps(run_payload),
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"ok": True, "session_id": session_key, "query_id": query_run.query_id}

    def save_workflow_state(self, payload: dict, *, state_key: str = "default") -> dict:
        state_key = str(state_key or "default").strip() or "default"
        conn = self._connect()
        try:
            self._ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO vibe_workflow_state (state_key, state_json)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE state_json = VALUES(state_json)
                    """,
                    (state_key, self._json_dumps(payload)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"ok": True, "state_key": state_key}

    def get_workflow_state(self, *, state_key: str = "default") -> dict | None:
        state_key = str(state_key or "default").strip() or "default"
        try:
            conn = self._connect()
            try:
                self._ensure_tables(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT state_json
                        FROM vibe_workflow_state
                        WHERE state_key = %s
                        """,
                        (state_key,),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return None
        if not row:
            return None
        return self._json_loads(row.get("state_json")) or None

    def save_workflow_stage(self, *, session_id: str, stage_key: str, payload: dict) -> dict:
        session_key = str(session_id or "").strip()
        stage = str(stage_key or "").strip()
        if not session_key or not stage:
            return {"ok": False, "reason": "session_id and stage_key are required"}
        conn = self._connect()
        try:
            self._ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO vibe_workflow_stage_state (session_id, stage_key, stage_json)
                    VALUES (%s, %s, %s)
                    ON DUPLICATE KEY UPDATE stage_json = VALUES(stage_json)
                    """,
                    (session_key, stage, self._json_dumps(payload)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return {"ok": True, "session_id": session_key, "stage_key": stage}

    def get_workflow_stage(self, *, session_id: str, stage_key: str) -> dict | None:
        session_key = str(session_id or "").strip()
        stage = str(stage_key or "").strip()
        if not session_key or not stage:
            return None
        try:
            conn = self._connect()
            try:
                self._ensure_tables(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT stage_json
                        FROM vibe_workflow_stage_state
                        WHERE session_id = %s AND stage_key = %s
                        """,
                        (session_key, stage),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return None
        if not row:
            return None
        return self._json_loads(row.get("stage_json")) or None

    def list_workflow_stages(self, *, session_id: str, limit: int = 200) -> list[dict]:
        session_key = str(session_id or "").strip()
        if not session_key:
            return []
        safe_limit = max(1, min(int(limit or 200), 1000))
        try:
            conn = self._connect()
            try:
                self._ensure_tables(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT stage_key, stage_json, updated_at, created_at
                        FROM vibe_workflow_stage_state
                        WHERE session_id = %s
                        ORDER BY updated_at DESC, created_at DESC
                        LIMIT {safe_limit}
                        """,
                        (session_key,),
                    )
                    rows = cur.fetchall()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return []
        return [
            {
                "stage_key": row.get("stage_key"),
                "stage": self._json_loads(row.get("stage_json")),
                "updated_at": row.get("updated_at"),
                "created_at": row.get("created_at"),
            }
            for row in rows
        ]

    def list_query_results(self, *, limit: int = 100) -> list[dict]:
        safe_limit = max(1, min(int(limit or 100), 500))
        try:
            conn = self._connect()
            try:
                self._ensure_tables(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        SELECT query_plan_json, query_run_json, updated_at, created_at
                        FROM vibe_query_run_state
                        ORDER BY updated_at DESC, created_at DESC
                        LIMIT {safe_limit}
                        """
                    )
                    rows = cur.fetchall()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return []
        results = []
        for row in rows:
            results.append(
                {
                    "query_plan": self._json_loads(row.get("query_plan_json")),
                    "query_run": self._json_loads(row.get("query_run_json")),
                    "updated_at": row.get("updated_at"),
                    "created_at": row.get("created_at"),
                }
            )
        return results

    def get_latest_query_result(self, session_id: str) -> dict | None:
        session_key = str(session_id or "").strip()
        if not session_key:
            return None
        try:
            conn = self._connect()
            try:
                self._ensure_tables(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT query_plan_json, query_run_json
                        FROM vibe_query_run_state
                        WHERE session_id = %s
                        ORDER BY updated_at DESC, created_at DESC
                        LIMIT 1
                        """,
                        (session_key,),
                    )
                    row = cur.fetchone()
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            return None
        if not row:
            return None
        return {
            "query_plan": self._json_loads(row.get("query_plan_json")),
            "query_run": self._json_loads(row.get("query_run_json")),
        }

    def delete_query_results_for_session(self, session_id: str) -> dict:
        session_key = str(session_id or "").strip()
        if not session_key:
            return {"ok": False, "deleted": 0, "reason": "session_id is required"}
        try:
            conn = self._connect()
            try:
                self._ensure_tables(conn)
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        DELETE FROM vibe_query_run_state
                        WHERE session_id = %s
                        """,
                        (session_key,),
                    )
                    deleted = int(cur.rowcount or 0)
                    cur.execute(
                        """
                        DELETE FROM vibe_workflow_stage_state
                        WHERE session_id = %s
                        """,
                        (session_key,),
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "deleted": 0, "reason": str(exc)}
        return {"ok": True, "deleted": deleted}

    def clear_runtime_state(self) -> dict:
        deleted: dict[str, int] = {}
        try:
            conn = self._connect()
            try:
                self._ensure_tables(conn)
                with conn.cursor() as cur:
                    for table_name in [
                        "vibe_query_run_state",
                        "vibe_workflow_stage_state",
                        "vibe_workflow_state",
                    ]:
                        cur.execute(f"DELETE FROM {table_name}")
                        deleted[table_name] = int(cur.rowcount or 0)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "deleted": deleted, "reason": str(exc)}
        return {"ok": True, "deleted": deleted}


runtime_persistence_service = RuntimePersistenceService()
