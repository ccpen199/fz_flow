from __future__ import annotations

import re
import os
from decimal import Decimal
from collections import Counter
from functools import lru_cache
from typing import Literal

import pymysql
from fastapi import APIRouter, HTTPException, Query

from ..services.field_value_resolver_service import field_value_resolver_service

router = APIRouter(prefix="/api/v1/clothing", tags=["clothing"])


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


def _db_connection() -> pymysql.connections.Connection:
    try:
        return pymysql.connect(**_mysql_config())
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"mysql connection failed: {exc}") from exc


def _normalize_decimal(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _split_list(value: str | None) -> list[str]:
    if not value:
        return []
    parts = re.split(r"\|\||[,，、]", str(value))
    return [part.strip() for part in parts if part and part.strip()]


@lru_cache(maxsize=1)
def _existing_table_names() -> set[str]:
    try:
        conn = _db_connection()
    except Exception:
        return set()
    try:
        cfg = _mysql_config()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME
                FROM information_schema.tables
                WHERE table_schema = %s
                """,
                (cfg["database"],),
            )
            return {row["TABLE_NAME"] for row in cur.fetchall()}
    except Exception:
        return set()
    finally:
        conn.close()


def _table_exists(table_name: str) -> bool:
    return str(table_name or "").strip() in _existing_table_names()


@lru_cache(maxsize=None)
def _table_column_names(table_name: str) -> set[str]:
    table_name = str(table_name or "").strip()
    if not table_name or not _table_exists(table_name):
        return set()
    try:
        conn = _db_connection()
    except Exception:
        return set()
    try:
        cfg = _mysql_config()
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name = %s
                """,
                (cfg["database"], table_name),
            )
            return {row["COLUMN_NAME"] for row in cur.fetchall()}
    except Exception:
        return set()
    finally:
        conn.close()


def _normalize_multi_value_text(value: str | None) -> str:
    text = str(value or "")
    text = text.replace("，", ",").replace("、", ",")
    text = re.sub(r"\s+", "", text)
    return text.strip(",")


def _explode_multi_value_rows(rows: list[dict] | None) -> list[dict]:
    counter: Counter[str] = Counter()
    for row in rows or []:
        raw_value = str(row.get("value") or "").strip()
        if not raw_value:
            continue
        count = int(row.get("count") or 0)
        for item in _split_list(raw_value):
            counter[item] += count
    return [{"value": value, "count": count} for value, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))]


def _canonical_lookup_value(
    *,
    table_name: str,
    field_name: str,
    value: str,
    semantic_name: str,
) -> str:
    resolved = field_value_resolver_service.resolve_field_value(
        table_name=table_name,
        field_name=field_name,
        raw_value=value,
        semantic_name=semantic_name,
    )
    return str(resolved.get("canonical_value") or value) if resolved.get("resolved") else value


def _build_item_filters(
    *,
    brand: str | None,
    category: str | None,
    sub_category: str | None,
    scene: str | None,
    fiber: str | None,
    min_price: float | None,
    max_price: float | None,
    ignore: Literal["brand", "category", "sub_category", "scene", "fiber"] | None = None,
) -> tuple[str, list]:
    where = " WHERE 1=1"
    params: list = []

    if brand and ignore != "brand":
        where += " AND ci.BrandName = %s"
        params.append(
            _canonical_lookup_value(
                table_name="clothing_info",
                field_name="BrandName",
                value=brand,
                semantic_name="品牌",
            )
        )

    if category and ignore != "category":
        where += " AND ci.Category = %s"
        params.append(
            _canonical_lookup_value(
                table_name="clothing_info",
                field_name="Category",
                value=category,
                semantic_name="一级类目",
            )
        )

    if sub_category and ignore != "sub_category":
        where += " AND ci.SubCategory = %s"
        params.append(
            _canonical_lookup_value(
                table_name="clothing_info",
                field_name="SubCategory",
                value=sub_category,
                semantic_name="二级类目",
            )
        )

    if min_price is not None:
        where += " AND ci.Price >= %s"
        params.append(min_price)

    if max_price is not None:
        where += " AND ci.Price <= %s"
        params.append(max_price)

    if scene and ignore != "scene":
        scene_value = _canonical_lookup_value(
            table_name="clothing_scene_info" if _table_exists("clothing_scene_info") else "clothing_info",
            field_name="Scene" if _table_exists("clothing_scene_info") else "SuitableScene",
            value=scene,
            semantic_name="场景",
        )
        if _table_exists("clothing_scene_info"):
            where += " AND EXISTS (SELECT 1 FROM clothing_scene_info s WHERE s.ClothingId = ci.Id AND s.Scene = %s)"
            params.append(scene_value)
        else:
            where += (
                " AND CONCAT(',', REPLACE(REPLACE(REPLACE(COALESCE(ci.SuitableScene, ''), '，', ','), '、', ','), ' ', ''), ',') "
                "LIKE CONCAT('%,', %s, ',%')"
            )
            params.append(_normalize_multi_value_text(scene_value))

    if fiber and ignore != "fiber":
        where += (
            " AND EXISTS ("
            "SELECT 1 FROM clothing_fiber_info f "
            "JOIN dict_fiber_info d ON d.Code = f.Code "
            "WHERE f.ClothingId = ci.Id AND d.Name = %s"
            ")"
        )
        params.append(
            _canonical_lookup_value(
                table_name="dict_fiber_info",
                field_name="Name",
                value=fiber,
                semantic_name="标准纤维名称",
            )
        )

    return where, params


@router.get("/items")
def list_clothing_items(
    brand: str | None = Query(default=None),
    category: str | None = Query(default=None),
    sub_category: str | None = Query(default=None),
    scene: str | None = Query(default=None),
    fiber: str | None = Query(default=None),
    min_price: float | None = Query(default=None),
    max_price: float | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict:
    scene_table_exists = _table_exists("clothing_scene_info")
    if scene_table_exists:
        scene_list_sql = """      (
        SELECT GROUP_CONCAT(DISTINCT csi.Scene SEPARATOR '||')
        FROM clothing_scene_info csi
        WHERE csi.ClothingId = ci.Id
      ) AS SceneList,
"""
    else:
        scene_list_sql = "      ci.SuitableScene AS SceneList,\n"
    sql = f"""
    SELECT
      ci.Id,
      ci.Name,
      ci.BrandName,
      ci.Price,
      ci.Category,
      ci.SubCategory,
      ci.TertiaryCategory,
      ci.LeafCategory,
      ci.ColorName,
      ci.ImageURL,
{scene_list_sql}      (
        SELECT GROUP_CONCAT(DISTINCT d.Name SEPARATOR '||')
        FROM clothing_fiber_info cfi
        JOIN dict_fiber_info d ON d.Code = cfi.Code
        WHERE cfi.ClothingId = ci.Id
      ) AS FiberList
    FROM clothing_info ci
    """
    where_sql, where_params = _build_item_filters(
        brand=brand,
        category=category,
        sub_category=sub_category,
        scene=scene,
        fiber=fiber,
        min_price=min_price,
        max_price=max_price,
    )
    sql += where_sql

    count_sql = f"SELECT COUNT(1) AS total FROM clothing_info ci{where_sql}"
    params = list(where_params)
    count_params = list(where_params)

    sql += " ORDER BY ci.Id DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    conn = _db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(count_sql, count_params)
            total = cur.fetchone()["total"]

            cur.execute(sql, params)
            rows = cur.fetchall()
    finally:
        conn.close()

    for row in rows:
        row["Price"] = _normalize_decimal(row.get("Price"))
        if scene_table_exists:
            row["SceneList"] = _split_list(row.get("SceneList"))
        else:
            row["SceneList"] = _split_list(row.get("SceneList"))
        row["FiberList"] = _split_list(row.get("FiberList"))

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": rows,
    }


@router.get("/facets")
def get_clothing_facets(
    brand: str | None = Query(default=None),
    category: str | None = Query(default=None),
    sub_category: str | None = Query(default=None),
    scene: str | None = Query(default=None),
    fiber: str | None = Query(default=None),
    min_price: float | None = Query(default=None),
    max_price: float | None = Query(default=None),
) -> dict:
    scene_table_exists = _table_exists("clothing_scene_info")
    conn = _db_connection()
    try:
        with conn.cursor() as cur:
            where_brand, params_brand = _build_item_filters(
                brand=brand,
                category=category,
                sub_category=sub_category,
                scene=scene,
                fiber=fiber,
                min_price=min_price,
                max_price=max_price,
                ignore="brand",
            )
            cur.execute(
                f"""
                SELECT ci.BrandName AS value, COUNT(1) AS count
                FROM clothing_info ci
                {where_brand} AND ci.BrandName IS NOT NULL AND ci.BrandName <> ''
                GROUP BY ci.BrandName
                ORDER BY count DESC, value ASC
                LIMIT 50
                """,
                params_brand,
            )
            brands = cur.fetchall()

            where_category, params_category = _build_item_filters(
                brand=brand,
                category=category,
                sub_category=sub_category,
                scene=scene,
                fiber=fiber,
                min_price=min_price,
                max_price=max_price,
                ignore="category",
            )
            cur.execute(
                f"""
                SELECT ci.Category AS value, COUNT(1) AS count
                FROM clothing_info ci
                {where_category} AND ci.Category IS NOT NULL AND ci.Category <> ''
                GROUP BY ci.Category
                ORDER BY count DESC, value ASC
                LIMIT 50
                """,
                params_category,
            )
            categories = cur.fetchall()

            where_sub_category, params_sub_category = _build_item_filters(
                brand=brand,
                category=category,
                sub_category=sub_category,
                scene=scene,
                fiber=fiber,
                min_price=min_price,
                max_price=max_price,
                ignore="sub_category",
            )
            cur.execute(
                f"""
                SELECT ci.SubCategory AS value, COUNT(1) AS count
                FROM clothing_info ci
                {where_sub_category} AND ci.SubCategory IS NOT NULL AND ci.SubCategory <> ''
                GROUP BY ci.SubCategory
                ORDER BY count DESC, value ASC
                LIMIT 100
                """,
                params_sub_category,
            )
            sub_categories = cur.fetchall()

            where_scene, params_scene = _build_item_filters(
                brand=brand,
                category=category,
                sub_category=sub_category,
                scene=scene,
                fiber=fiber,
                min_price=min_price,
                max_price=max_price,
                ignore="scene",
            )
            if scene_table_exists:
                cur.execute(
                    f"""
                    SELECT s.Scene AS value, COUNT(DISTINCT ci.Id) AS count
                    FROM clothing_info ci
                    JOIN clothing_scene_info s ON s.ClothingId = ci.Id
                    {where_scene} AND s.Scene IS NOT NULL AND s.Scene <> ''
                    GROUP BY s.Scene
                    ORDER BY count DESC, value ASC
                    LIMIT 100
                    """,
                    params_scene,
                )
                scenes = cur.fetchall()
            else:
                cur.execute(
                    f"""
                    SELECT ci.SuitableScene AS value, COUNT(1) AS count
                    FROM clothing_info ci
                    {where_scene} AND ci.SuitableScene IS NOT NULL AND ci.SuitableScene <> ''
                    GROUP BY ci.SuitableScene
                    ORDER BY count DESC, value ASC
                    LIMIT 100
                    """,
                    params_scene,
                )
                scenes = _explode_multi_value_rows(cur.fetchall())

            where_fiber, params_fiber = _build_item_filters(
                brand=brand,
                category=category,
                sub_category=sub_category,
                scene=scene,
                fiber=fiber,
                min_price=min_price,
                max_price=max_price,
                ignore="fiber",
            )
            cur.execute(
                f"""
                SELECT d.Name AS value, COUNT(DISTINCT ci.Id) AS count
                FROM clothing_info ci
                JOIN clothing_fiber_info f ON f.ClothingId = ci.Id
                JOIN dict_fiber_info d ON d.Code = f.Code
                {where_fiber} AND d.Name IS NOT NULL AND d.Name <> ''
                GROUP BY d.Name
                ORDER BY count DESC, value ASC
                LIMIT 100
                """,
                params_fiber,
            )
            fibers = cur.fetchall()
    finally:
        conn.close()

    return {
        "brand": brands,
        "category": categories,
        "sub_category": sub_categories,
        "scene": scenes,
        "fiber": fibers,
    }


@router.get("/items/{clothing_id}")
def get_clothing_item(clothing_id: int) -> dict:
    scene_table_exists = _table_exists("clothing_scene_info")
    functions_table_exists = _table_exists("clothing_functions_info")
    pattern_table_exists = _table_exists("clothing_pattern_info")
    texture_table_exists = _table_exists("clothing_texture_info")
    color_table_exists = _table_exists("clothing_images_color")
    conn = _db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  Id,
                  Name,
                  NameEn,
                  BrandName,
                  Price,
                  OriginalPrice,
                  Category,
                  SubCategory,
                  TertiaryCategory,
                  QuaternaryCategory,
                  LeafCategory,
                  SuitableScene,
                  SuitableSeason,
                  SuitableGender,
                  SuitableAge,
                  ColorName,
                  ColorCode,
                  ImageURL,
                  SourceUrl,
                  Functions,
                  Technologies,
                  OtherFunctions,
                  OtherFeatures,
                  Pattern,
                  CreateTime
                FROM clothing_info
                WHERE Id = %s
                """,
                (clothing_id,),
            )
            base = cur.fetchone()
            if not base:
                raise HTTPException(status_code=404, detail="clothing item not found")

            cur.execute(
                """
                SELECT
                  f.Code,
                  d.Name,
                  f.Percent
                FROM clothing_fiber_info f
                LEFT JOIN dict_fiber_info d
                  ON d.Code = f.Code
                WHERE f.ClothingId = %s
                ORDER BY f.Percent DESC, f.Id ASC
                """,
                (clothing_id,),
            )
            fibers = cur.fetchall()

            if functions_table_exists:
                cur.execute(
                    """
                    SELECT Functionality
                    FROM clothing_functions_info
                    WHERE ClothingId = %s
                    ORDER BY Id ASC
                    """,
                    (clothing_id,),
                )
                functions = [item["Functionality"] for item in cur.fetchall() if item.get("Functionality")]
            else:
                functions = []
                for key in ("Functions", "OtherFunctions", "Technologies"):
                    functions.extend(_split_list(base.get(key)))
                functions = list(dict.fromkeys(functions))

            if scene_table_exists:
                cur.execute(
                    """
                    SELECT Scene
                    FROM clothing_scene_info
                    WHERE ClothingId = %s
                    ORDER BY Id ASC
                    """,
                    (clothing_id,),
                )
                scenes = [item["Scene"] for item in cur.fetchall() if item.get("Scene")]
            else:
                scenes = _split_list(base.get("SuitableScene"))

            patterns = []
            if pattern_table_exists:
                pattern_columns = _table_column_names("clothing_pattern_info")
                pattern_value_expr = "pattern" if "pattern" in pattern_columns else "Name" if "Name" in pattern_columns else ""
                reason_expr = "IdentifyReason" if "IdentifyReason" in pattern_columns else "NULL"
                confidence_expr = "Confidence" if "Confidence" in pattern_columns else "NULL"
                if pattern_value_expr:
                    cur.execute(
                        f"""
                        SELECT
                          {pattern_value_expr} AS pattern,
                          {reason_expr} AS IdentifyReason,
                          {confidence_expr} AS Confidence
                        FROM clothing_pattern_info
                        WHERE ClothingId = %s
                        ORDER BY Id ASC
                        """,
                        (clothing_id,),
                    )
                    patterns = cur.fetchall()
            if not patterns and base.get("Pattern"):
                patterns = [{"pattern": base.get("Pattern"), "IdentifyReason": None, "Confidence": None}]

            if texture_table_exists:
                cur.execute(
                    """
                    SELECT Texture, FabricType, PatternLayout, PatternTechnique, PatternComposition, PatternDefinition, PatternStyle
                    FROM clothing_texture_info
                    WHERE ClothingId = %s
                    ORDER BY Id ASC
                    """,
                    (clothing_id,),
                )
                textures = cur.fetchall()
            else:
                textures = []

            if color_table_exists:
                cur.execute(
                    """
                    SELECT RGB, LAB, ColoroId, PantoneId, Percent
                    FROM clothing_images_color
                    WHERE ClothingId = %s
                    ORDER BY Percent DESC, Id ASC
                    """,
                    (clothing_id,),
                )
                colors = cur.fetchall()
            else:
                colors = []
    finally:
        conn.close()

    base["Price"] = _normalize_decimal(base.get("Price"))
    for fiber_row in fibers:
        fiber_row["Percent"] = _normalize_decimal(fiber_row.get("Percent"))
    for color_row in colors:
        color_row["Percent"] = _normalize_decimal(color_row.get("Percent"))

    return {
        "base": base,
        "fiber": fibers,
        "functions": functions,
        "scenes": scenes,
        "pattern": patterns,
        "texture": textures,
        "colors": colors,
    }
