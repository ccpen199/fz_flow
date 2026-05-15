#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import pymysql


TABLES = [
    "clothing_info",
    "clothing_fiber_info",
    "clothing_functions_info",
    "clothing_images_color",
    "clothing_pattern_info",
    "clothing_scene_info",
    "clothing_texture_info",
]


def split_sql_statements(sql_text: str) -> list[str]:
    statements: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    in_backtick = False
    escaped = False

    for ch in sql_text:
        buf.append(ch)
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "'" and not in_double and not in_backtick:
            in_single = not in_single
            continue
        if ch == '"' and not in_single and not in_backtick:
            in_double = not in_double
            continue
        if ch == "`" and not in_single and not in_double:
            in_backtick = not in_backtick
            continue

        if ch == ";" and not in_single and not in_double and not in_backtick:
            statement = "".join(buf).strip()
            if statement:
                statements.append(statement)
            buf = []

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)

    return statements


def run_sql_file(cur: pymysql.cursors.Cursor, path: Path) -> tuple[int, int]:
    sql_text = path.read_text(encoding="utf-8")
    statements = split_sql_statements(sql_text)
    executed = 0
    failed = 0

    for idx, statement in enumerate(statements, start=1):
        stripped = statement.strip()
        if not stripped or stripped.startswith("--"):
            continue
        try:
            cur.execute(statement)
            executed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[WARN] statement #{idx} failed: {exc}")
        if idx % 200 == 0:
            print(f"[INFO] processed {idx}/{len(statements)} statements")

    return executed, failed


def add_indexes(conn: pymysql.connections.Connection, patch_file: Path) -> None:
    with conn.cursor() as cur:
        for statement in split_sql_statements(patch_file.read_text(encoding="utf-8")):
            sql = statement.strip()
            if not sql or sql.startswith("--"):
                continue
            try:
                cur.execute(sql)
            except pymysql.err.OperationalError as exc:
                if exc.args and exc.args[0] == 1061:
                    print(f"[INFO] index already exists, skip: {exc}")
                    continue
                raise


def print_row_counts(cur: pymysql.cursors.Cursor) -> None:
    print("[INFO] row counts after import")
    for table in TABLES:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        count = cur.fetchone()[0]
        print(f"  - {table}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Import dataservice_test SQL into local MySQL dataservice_test_local")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3306)
    parser.add_argument("--user", default="root")
    parser.add_argument("--password", default="root")
    parser.add_argument("--database", default="dataservice_test_local")
    parser.add_argument("--sql-file", default="docs/Sql/dataservice_test(1).sql")
    parser.add_argument("--index-file", default="scripts/mysql_index_patch.sql")
    args = parser.parse_args()

    sql_file = Path(args.sql_file)
    index_file = Path(args.index_file)

    if not sql_file.exists():
        raise FileNotFoundError(f"sql file not found: {sql_file}")
    if not index_file.exists():
        raise FileNotFoundError(f"index patch file not found: {index_file}")

    root_conn = pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        autocommit=True,
        charset="utf8mb4",
    )
    with root_conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS `{args.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin"
        )
    root_conn.close()

    conn = pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        autocommit=False,
        charset="utf8mb4",
    )

    try:
        with conn.cursor() as cur:
            executed, failed = run_sql_file(cur, sql_file)
            conn.commit()
            print(f"[INFO] import done: executed={executed}, failed={failed}")

        add_indexes(conn, index_file)
        conn.commit()

        with conn.cursor() as cur:
            print_row_counts(cur)

        print(f"[INFO] database ready: {args.database}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
