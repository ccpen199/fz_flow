import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
CONFIG_PATH = DATA_DIR / "config_store.json"

PRESET_SCENES: List[Dict[str, Any]] = [
    {
        "name": "竞品与价格分析",
        "description": "围绕品牌/平台/品类/价格带做竞品价格结构与折扣趋势分析。",
        "sample_goals": [
            "最近30天各品牌连衣裙的价格带分布如何？",
            "哪些品牌近期在羽绒服品类的促销力度明显加大？",
            "某平台中 Zara 与优衣库的外套价格结构差异是什么？",
            "近60天各平台外套的平均折扣率变化趋势如何？",
        ],
        "fields": [
            {"table_name": "product_snapshot", "field_name": "snapshot_date", "semantic_name": "快照日期", "description": "按天趋势分析时间轴", "role": "time", "enabled": True},
            {"table_name": "product_snapshot", "field_name": "sale_price", "semantic_name": "销售价", "description": "分析实际成交价/促销价", "role": "metric", "enabled": True},
            {"table_name": "product_snapshot", "field_name": "listed_price", "semantic_name": "吊牌价", "description": "折扣口径基准价格", "role": "metric", "enabled": True},
            {"table_name": "dim_brand", "field_name": "brand_name", "semantic_name": "品牌", "description": "品牌维度", "role": "dimension", "enabled": True},
            {"table_name": "dim_platform", "field_name": "platform_name", "semantic_name": "平台", "description": "平台维度", "role": "dimension", "enabled": True},
            {"table_name": "dim_category", "field_name": "category_name", "semantic_name": "品类", "description": "品类维度", "role": "dimension", "enabled": True},
        ],
    },
    {
        "name": "商品结构分析",
        "description": "分析品类结构、SKU丰富度、品牌和平台上的商品布局差异。",
        "sample_goals": [
            "某品牌女装商品结构是否过于集中？",
            "哪些品牌在牛仔裤品类的 SKU 最丰富？",
            "各平台春季新品的颜色结构有什么差异？",
            "不同平台在外套品类的SKU结构差异如何？",
        ],
        "fields": [
            {"table_name": "product_snapshot", "field_name": "sku_id", "semantic_name": "SKU", "description": "用于统计SKU丰富度", "role": "dimension", "enabled": True},
            {"table_name": "product_snapshot", "field_name": "stock_qty", "semantic_name": "库存量", "description": "可用于结构权重估计", "role": "metric", "enabled": True},
            {"table_name": "dim_brand", "field_name": "brand_name", "semantic_name": "品牌", "description": "品牌维度", "role": "dimension", "enabled": True},
            {"table_name": "dim_platform", "field_name": "platform_name", "semantic_name": "平台", "description": "平台维度", "role": "dimension", "enabled": True},
            {"table_name": "dim_category", "field_name": "category_name", "semantic_name": "品类", "description": "品类维度", "role": "dimension", "enabled": True},
        ],
    },
    {
        "name": "趋势与爆款分析",
        "description": "关注上新节奏、价格波动、异常变化和潜在爆款识别。",
        "sample_goals": [
            "哪些品牌最近两周上新节奏明显加快？",
            "哪些品牌最近60天价格波动最明显？",
            "哪些品类在近30天出现异常变化？",
            "近30天潜在爆款SKU有哪些，依据是什么？",
        ],
        "fields": [
            {"table_name": "product_snapshot", "field_name": "snapshot_date", "semantic_name": "快照日期", "description": "趋势主时间轴", "role": "time", "enabled": True},
            {"table_name": "product_snapshot", "field_name": "is_new", "semantic_name": "是否上新", "description": "上新节奏分析标识", "role": "filter", "enabled": True},
            {"table_name": "product_snapshot", "field_name": "sale_price", "semantic_name": "销售价", "description": "波动分析核心指标", "role": "metric", "enabled": True},
            {"table_name": "product_snapshot", "field_name": "sku_id", "semantic_name": "SKU", "description": "爆款候选统计单元", "role": "dimension", "enabled": True},
            {"table_name": "dim_brand", "field_name": "brand_name", "semantic_name": "品牌", "description": "品牌维度", "role": "dimension", "enabled": True},
            {"table_name": "dim_category", "field_name": "category_name", "semantic_name": "品类", "description": "品类维度", "role": "dimension", "enabled": True},
        ],
    },
    {
        "name": "商品价格分析",
        "description": "对单品牌/多品牌在不同平台和时间窗口下的价格表现做总览。",
        "sample_goals": [
            "最近30天女装外套在不同平台和品牌的价格结构变化如何？",
            "某品牌在各平台的价格分布和折扣策略是否一致？",
            "近14天平台维度下平均销售价变化趋势如何？",
        ],
        "fields": [
            {"table_name": "product_snapshot", "field_name": "snapshot_date", "semantic_name": "快照日期", "description": "时间筛选", "role": "time", "enabled": True},
            {"table_name": "product_snapshot", "field_name": "sale_price", "semantic_name": "销售价", "description": "核心价格指标", "role": "metric", "enabled": True},
            {"table_name": "dim_brand", "field_name": "brand_name", "semantic_name": "品牌", "description": "品牌维度", "role": "dimension", "enabled": True},
            {"table_name": "dim_platform", "field_name": "platform_name", "semantic_name": "平台", "description": "平台维度", "role": "dimension", "enabled": True},
        ],
    },
]

PRESET_RELATIONS: List[Dict[str, str]] = [
    {
        "left_table": "product_snapshot",
        "left_field": "brand_id",
        "right_table": "dim_brand",
        "right_field": "brand_id",
        "join_type": "INNER",
        "note": "品牌维表关联",
    },
    {
        "left_table": "product_snapshot",
        "left_field": "platform_id",
        "right_table": "dim_platform",
        "right_field": "platform_id",
        "join_type": "INNER",
        "note": "平台维表关联",
    },
    {
        "left_table": "product_snapshot",
        "left_field": "category_id",
        "right_table": "dim_category",
        "right_field": "category_id",
        "join_type": "INNER",
        "note": "品类维表关联",
    },
]

def _default_store() -> Dict[str, Any]:
    return {"scenes": []}


def _read_store() -> Dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        data = _default_store()
        CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _write_store(data: Dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_scenes() -> List[Dict[str, Any]]:
    return _read_store()["scenes"]


def ensure_preset_scenes() -> List[Dict[str, Any]]:
    data = _read_store()
    existing_names = {scene.get("name", "") for scene in data["scenes"]}
    preset_by_name = {item["name"]: item for item in PRESET_SCENES}
    changed = False

    for scene in data["scenes"]:
        preset = preset_by_name.get(scene.get("name", ""))
        if not preset:
            continue
        if "sample_goals" not in scene or not scene.get("sample_goals"):
            scene["sample_goals"] = preset.get("sample_goals", [])
            changed = True

    for preset in PRESET_SCENES:
        if preset["name"] in existing_names:
            continue
        scene = {
            "scene_id": uuid4().hex[:10],
            "name": preset["name"],
            "description": preset["description"],
            "sample_goals": preset.get("sample_goals", []),
            "fields": [],
            "relations": [],
        }
        for field in preset.get("fields", []):
            scene["fields"].append(
                {
                    "field_id": uuid4().hex[:12],
                    **field,
                }
            )
        for relation in PRESET_RELATIONS:
            scene["relations"].append(
                {
                    "relation_id": uuid4().hex[:12],
                    **relation,
                }
            )
        data["scenes"].append(scene)
        changed = True
    if changed:
        _write_store(data)
    return data["scenes"]


def create_scene(name: str, description: str) -> Dict[str, Any]:
    data = _read_store()
    scene = {
        "scene_id": uuid4().hex[:10],
        "name": name.strip(),
        "description": description.strip(),
        "fields": [],
        "relations": [],
    }
    data["scenes"].append(scene)
    _write_store(data)
    return scene


def get_scene(scene_id: str) -> Optional[Dict[str, Any]]:
    scenes = _read_store()["scenes"]
    return next((s for s in scenes if s["scene_id"] == scene_id), None)


def add_field(
    scene_id: str,
    table_name: str,
    field_name: str,
    semantic_name: str,
    description: str,
    role: str,
    enabled: bool,
) -> Dict[str, Any]:
    data = _read_store()
    scene = next((s for s in data["scenes"] if s["scene_id"] == scene_id), None)
    if scene is None:
        raise ValueError("scene not found")
    field = {
        "field_id": uuid4().hex[:12],
        "table_name": table_name.strip(),
        "field_name": field_name.strip(),
        "semantic_name": semantic_name.strip(),
        "description": description.strip(),
        "role": role.strip() or "dimension",
        "enabled": bool(enabled),
    }
    scene["fields"].append(field)
    _write_store(data)
    return field


def add_relation(
    scene_id: str,
    left_table: str,
    left_field: str,
    right_table: str,
    right_field: str,
    join_type: str,
    note: str,
) -> Dict[str, Any]:
    data = _read_store()
    scene = next((s for s in data["scenes"] if s["scene_id"] == scene_id), None)
    if scene is None:
        raise ValueError("scene not found")
    relation = {
        "relation_id": uuid4().hex[:12],
        "left_table": left_table.strip(),
        "left_field": left_field.strip(),
        "right_table": right_table.strip(),
        "right_field": right_field.strip(),
        "join_type": (join_type or "INNER").strip().upper(),
        "note": note.strip(),
    }
    scene["relations"].append(relation)
    _write_store(data)
    return relation
