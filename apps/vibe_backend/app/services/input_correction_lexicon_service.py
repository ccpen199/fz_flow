from __future__ import annotations

import os
import threading
import unicodedata
from uuid import uuid4

import pymysql


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


def normalize_correction_word(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    return "".join(char for char in text if char.isalnum())


class InputCorrectionLexiconService:
    """Stores user-maintained target spellings for pre-execution input correction."""

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
                    CREATE TABLE IF NOT EXISTS vibe_input_correction_lexicon (
                      correction_id VARCHAR(64) PRIMARY KEY,
                      correct_word VARCHAR(255) NOT NULL,
                      normalized_word VARCHAR(255) NOT NULL,
                      enabled TINYINT(1) NOT NULL DEFAULT 1,
                      note VARCHAR(500) NOT NULL DEFAULT '',
                      created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                      updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                      UNIQUE KEY uq_input_correction_normalized (normalized_word),
                      INDEX idx_input_correction_enabled_updated (enabled, updated_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                    """
                )
            conn.commit()
            self._tables_ready = True

    def ensure(self) -> None:
        conn = self._connect()
        try:
            self._ensure_tables(conn)
        finally:
            conn.close()

    @staticmethod
    def _row_payload(row: dict | None) -> dict:
        row = row or {}
        return {
            "correction_id": str(row.get("correction_id") or "").strip(),
            "correct_word": str(row.get("correct_word") or "").strip(),
            "normalized_word": str(row.get("normalized_word") or "").strip(),
            "enabled": bool(row.get("enabled", 1)),
            "note": str(row.get("note") or "").strip(),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
        }

    def list_words(self, *, include_disabled: bool = False) -> list[dict]:
        query = """
            SELECT correction_id, correct_word, normalized_word, enabled, note, created_at, updated_at
            FROM vibe_input_correction_lexicon
        """
        params: list[object] = []
        if not include_disabled:
            query += "\nWHERE enabled = 1"
        query += "\nORDER BY updated_at DESC, created_at DESC, correct_word ASC"
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
        return [self._row_payload(row) for row in rows]

    def upsert_word(self, correct_word: str, *, note: str = "") -> dict:
        word = str(correct_word or "").strip()
        normalized = normalize_correction_word(word)
        if not word:
            raise ValueError("correct_word is required")
        if len(word) > 255:
            raise ValueError("correct_word must be at most 255 characters")
        if len(note) > 500:
            raise ValueError("note must be at most 500 characters")
        if not normalized:
            raise ValueError("correct_word must contain letters, numbers, or Chinese characters")

        correction_id = f"corr_{uuid4().hex[:12]}"
        conn = self._connect()
        try:
            self._ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO vibe_input_correction_lexicon (
                      correction_id, correct_word, normalized_word, enabled, note
                    )
                    VALUES (%s, %s, %s, 1, %s)
                    ON DUPLICATE KEY UPDATE
                      correct_word = VALUES(correct_word),
                      enabled = 1,
                      note = VALUES(note)
                    """,
                    (correction_id, word, normalized, str(note or "").strip()),
                )
                cur.execute(
                    """
                    SELECT correction_id, correct_word, normalized_word, enabled, note, created_at, updated_at
                    FROM vibe_input_correction_lexicon
                    WHERE normalized_word = %s
                    """,
                    (normalized,),
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
        return self._row_payload(row)

    def set_enabled(self, correction_id: str, enabled: bool) -> dict | None:
        key = str(correction_id or "").strip()
        if not key:
            return None
        conn = self._connect()
        try:
            self._ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE vibe_input_correction_lexicon
                    SET enabled = %s
                    WHERE correction_id = %s
                    """,
                    (1 if enabled else 0, key),
                )
                if not cur.rowcount:
                    conn.rollback()
                    return None
                cur.execute(
                    """
                    SELECT correction_id, correct_word, normalized_word, enabled, note, created_at, updated_at
                    FROM vibe_input_correction_lexicon
                    WHERE correction_id = %s
                    """,
                    (key,),
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
        return self._row_payload(row)

    def delete_word(self, correction_id: str) -> bool:
        key = str(correction_id or "").strip()
        if not key:
            return False
        conn = self._connect()
        try:
            self._ensure_tables(conn)
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM vibe_input_correction_lexicon WHERE correction_id = %s",
                    (key,),
                )
                affected = int(cur.rowcount or 0)
            conn.commit()
        finally:
            conn.close()
        return affected > 0


input_correction_lexicon_service = InputCorrectionLexiconService()
