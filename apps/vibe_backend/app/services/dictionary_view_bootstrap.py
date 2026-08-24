from __future__ import annotations

import os
import threading

import pymysql


_FIBER_ROOT_PARENT_ID = "46641D1E-D348-4503-8C60-1664213D4D19"


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


class DictionaryViewBootstrap:
    _lock = threading.Lock()
    _ready = False

    def _connect(self):
        return pymysql.connect(**_mysql_config())

    def ensure(self) -> None:
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            conn = self._connect()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        CREATE OR REPLACE VIEW `dict_fiber_info` AS
                        SELECT
                          `Id`,
                          `ParentId`,
                          `DictType`,
                          `DictValue` AS `Code`,
                          `DictName` AS `Name`
                        FROM `dict_info`
                        WHERE `ParentId` = '{_FIBER_ROOT_PARENT_ID}'
                        """
                    )
                    cur.execute(
                        """
                        CREATE OR REPLACE VIEW `dict_brand_info` AS
                        SELECT
                          `Id`,
                          `Type`,
                          `Code`,
                          `Name`,
                          `NameEn`,
                          `Alias`,
                          `Location`,
                          `Source`,
                          `SourceName`,
                          `SourceUrl`
                        FROM `dict_brand`
                        WHERE `Type` = 0
                        """
                    )
            finally:
                conn.close()
            self._ready = True


dictionary_view_bootstrap = DictionaryViewBootstrap()
