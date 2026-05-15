from __future__ import annotations

import os
from decimal import Decimal
from typing import Literal

import pymysql
from fastapi import APIRouter, HTTPException, Query

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
    return [part for part in value.split("||") if part]


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
        params.append(brand)

    if category and ignore != "category":
        where += " AND ci.Category = %s"
        params.append(category)

    if sub_category and ignore != "sub_category":
        where += " AND ci.SubCategory = %s"
        params.append(sub_category)

    if min_price is not None:
        where += " AND ci.Price >= %s"
        params.append(min_price)

    if max_price is not None:
        where += " AND ci.Price <= %s"
        params.append(max_price)

    if scene and ignore != "scene":
        where += " AND EXISTS (SELECT 1 FROM clothing_scene_info s WHERE s.ClothingId = ci.Id AND s.Scene = %s)"
        params.append(scene)

    if fiber and ignore != "fiber":
        where += " AND EXISTS (SELECT 1 FROM clothing_fiber_info f WHERE f.ClothingId = ci.Id AND f.Name = %s)"
        params.append(fiber)

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
    sql = """
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
      (
        SELECT GROUP_CONCAT(DISTINCT csi.Scene SEPARATOR '||')
        FROM clothing_scene_info csi
        WHERE csi.ClothingId = ci.Id
      ) AS SceneList,
      (
        SELECT GROUP_CONCAT(DISTINCT cfi.Name SEPARATOR '||')
        FROM clothing_fiber_info cfi
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
                SELECT f.Name AS value, COUNT(DISTINCT ci.Id) AS count
                FROM clothing_info ci
                JOIN clothing_fiber_info f ON f.ClothingId = ci.Id
                {where_fiber} AND f.Name IS NOT NULL AND f.Name <> ''
                GROUP BY f.Name
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
                SELECT Name, Percent
                FROM clothing_fiber_info
                WHERE ClothingId = %s
                ORDER BY Percent DESC, Id ASC
                """,
                (clothing_id,),
            )
            fibers = cur.fetchall()

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

            cur.execute(
                """
                SELECT pattern, IdentifyReason
                FROM clothing_pattern_info
                WHERE ClothingId = %s
                ORDER BY Id ASC
                """,
                (clothing_id,),
            )
            patterns = cur.fetchall()

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
