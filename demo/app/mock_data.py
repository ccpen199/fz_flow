import random
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Tuple

ROOT = Path(__file__).resolve().parents[2]
DB_DIR = ROOT / "data"
DB_PATH = DB_DIR / "demo.db"


def ensure_demo_database() -> Tuple[str, bool]:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    created = not DB_PATH.exists()
    if created:
        _create_schema()
        _seed_data()
    return str(DB_PATH), created


def _create_schema() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE dim_brand (
          brand_id INTEGER PRIMARY KEY,
          brand_name TEXT NOT NULL
        );
        CREATE TABLE dim_platform (
          platform_id INTEGER PRIMARY KEY,
          platform_name TEXT NOT NULL
        );
        CREATE TABLE dim_category (
          category_id INTEGER PRIMARY KEY,
          category_name TEXT NOT NULL
        );
        CREATE TABLE product_snapshot (
          snapshot_date TEXT NOT NULL,
          sku_id TEXT NOT NULL,
          brand_id INTEGER NOT NULL,
          platform_id INTEGER NOT NULL,
          category_id INTEGER NOT NULL,
          listed_price REAL NOT NULL,
          sale_price REAL NOT NULL,
          is_new INTEGER NOT NULL,
          stock_qty INTEGER NOT NULL
        );
        CREATE INDEX idx_snapshot_date ON product_snapshot(snapshot_date);
        CREATE INDEX idx_snapshot_dims ON product_snapshot(brand_id, platform_id, category_id);
        """
    )
    conn.commit()
    conn.close()


def _seed_data() -> None:
    rng = random.Random(42)
    brands = ["Zara", "Uniqlo", "H&M", "Only", "Peacebird", "Mo&Co"]
    platforms = ["Tmall", "JD", "Douyin", "WeChat", "Xiaohongshu"]
    categories = ["外套", "连衣裙", "牛仔裤", "羽绒服", "针织衫"]

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executemany(
        "INSERT INTO dim_brand(brand_id, brand_name) VALUES (?, ?)",
        [(i + 1, b) for i, b in enumerate(brands)],
    )
    cur.executemany(
        "INSERT INTO dim_platform(platform_id, platform_name) VALUES (?, ?)",
        [(i + 1, p) for i, p in enumerate(platforms)],
    )
    cur.executemany(
        "INSERT INTO dim_category(category_id, category_name) VALUES (?, ?)",
        [(i + 1, c) for i, c in enumerate(categories)],
    )

    sku_count = 800
    start = date.today() - timedelta(days=89)
    rows = []
    for d in range(90):
        snapshot = (start + timedelta(days=d)).isoformat()
        for sku_idx in range(1, sku_count + 1):
            brand_id = (sku_idx % len(brands)) + 1
            platform_id = (sku_idx % len(platforms)) + 1
            category_id = (sku_idx % len(categories)) + 1
            base_price = 99 + brand_id * 30 + category_id * 20 + rng.randint(-15, 15)
            discount = rng.choice([0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 1.0])
            listed_price = float(base_price)
            sale_price = round(listed_price * discount, 2)
            is_new = 1 if d < 30 and rng.random() < 0.22 else 0
            stock_qty = rng.randint(5, 500)
            rows.append(
                (
                    snapshot,
                    f"SKU-{sku_idx:05d}",
                    brand_id,
                    platform_id,
                    category_id,
                    listed_price,
                    sale_price,
                    is_new,
                    stock_qty,
                )
            )
        if len(rows) >= 10000:
            cur.executemany(
                """
                INSERT INTO product_snapshot(
                  snapshot_date, sku_id, brand_id, platform_id, category_id,
                  listed_price, sale_price, is_new, stock_qty
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            rows = []
    if rows:
        cur.executemany(
            """
            INSERT INTO product_snapshot(
              snapshot_date, sku_id, brand_id, platform_id, category_id,
              listed_price, sale_price, is_new, stock_qty
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.commit()
    conn.close()
