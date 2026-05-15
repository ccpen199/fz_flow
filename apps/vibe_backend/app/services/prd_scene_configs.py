from __future__ import annotations

from copy import deepcopy
from typing import Any

from .price_scene_config import (
    PRICE_SCENE_DESCRIPTION,
    PRICE_SCENE_FIELDS,
    PRICE_SCENE_ID,
    PRICE_SCENE_METRIC_TEMPLATES,
    PRICE_SCENE_NAME,
    PRICE_SCENE_OUT_OF_SCOPE,
    PRICE_SCENE_PRICE_BANDS,
    PRICE_SCENE_QUESTION_MATRIX,
    PRICE_SCENE_RELATIONS,
    PRICE_SCENE_SAMPLE_GOALS,
)


def _field(
    semantic_name: str,
    table_name: str,
    field_name: str,
    role: str,
    description: str,
    *,
    required: bool = False,
    aliases: list[str] | None = None,
    unit: str = "",
    aggregation: str = "",
    er_path: str = "",
) -> dict[str, Any]:
    return {
        "semantic_name": semantic_name,
        "table_name": table_name,
        "field_name": field_name,
        "role": role,
        "required": required,
        "description": description,
        "aliases": aliases or [semantic_name, field_name],
        "unit": unit,
        "aggregation": aggregation,
        "er_path": er_path,
    }


def _relation(
    left_table: str,
    left_field: str,
    right_table: str,
    right_field: str,
    join_type: str,
    note: str,
) -> dict[str, str]:
    return {
        "left_table": left_table,
        "left_field": left_field,
        "right_table": right_table,
        "right_field": right_field,
        "join_type": join_type,
        "note": note,
    }


def _requirement(
    semantic_name: str,
    table_name: str,
    field_name: str,
    role: str,
    purpose: str,
    *,
    required: bool = True,
) -> dict[str, Any]:
    return {
        "semantic_name": semantic_name,
        "table_name": table_name,
        "field_name": field_name,
        "role": role,
        "required": required,
        "purpose": purpose,
    }


COMMON_PRICE_BANDS = [
    {"band": "0-99", "min": 0, "max": 99},
    {"band": "100-199", "min": 100, "max": 199},
    {"band": "200-399", "min": 200, "max": 399},
    {"band": "400-799", "min": 400, "max": 799},
    {"band": "800+", "min": 800, "max": None},
]


COMMON_METRIC_TEMPLATES = [
    {
        "name": "SKU数",
        "formula": "COUNT(DISTINCT clothing_info.Id)",
        "description": "按商品ID去重后的商品数量。",
    },
    {
        "name": "平均价",
        "formula": "AVG(clothing_info.Price)",
        "description": "分组内商品销售价均值。",
    },
    {
        "name": "最低价",
        "formula": "MIN(clothing_info.Price)",
        "description": "分组内最低销售价。",
    },
    {
        "name": "最高价",
        "formula": "MAX(clothing_info.Price)",
        "description": "分组内最高销售价。",
    },
    {
        "name": "价格跨度",
        "formula": "MAX(clothing_info.Price) - MIN(clothing_info.Price)",
        "description": "分组内最高价与最低价的差值。",
    },
    {
        "name": "价格带SKU数",
        "formula": "COUNT(DISTINCT clothing_info.Id) grouped by fixed price band",
        "description": "按固定价格带分桶后的去重商品数。",
    },
    {
        "name": "组内占比",
        "formula": "COUNT(DISTINCT clothing_info.Id) / SUM(COUNT(DISTINCT clothing_info.Id)) OVER(PARTITION BY group)",
        "description": "品牌、类目或日期组内的SKU占比。",
    },
    {
        "name": "视觉元素覆盖数",
        "formula": "COUNT(DISTINCT visual_dimension)",
        "description": "图案、肌理、织造方式、工艺类型或色号的覆盖数量。",
    },
    {
        "name": "主色占比",
        "formula": "MAX(clothing_images_color.Percent)",
        "description": "图片识别颜色中的最大占比，用于近似商品主色。",
    },
]


VISUAL_ANALYSIS_FIELDS = [
    _field("图片RGB", "clothing_images_color", "RGB", "dimension", "图片识别出的RGB颜色值。", aliases=["RGB", "图片RGB", "图片颜色RGB"], er_path="clothing_info.Id = clothing_images_color.ClothingId"),
    _field("图片LAB", "clothing_images_color", "LAB", "dimension", "图片识别出的LAB颜色值。", aliases=["LAB", "图片LAB"], er_path="clothing_info.Id = clothing_images_color.ClothingId"),
    _field("Coloro色号", "clothing_images_color", "ColoroId", "dimension", "图片识别出的Coloro色号。", aliases=["Coloro", "ColoroId", "Coloro色号"], er_path="clothing_info.Id = clothing_images_color.ClothingId"),
    _field("Pantone色号", "clothing_images_color", "PantoneId", "dimension", "图片识别出的Pantone色号。", aliases=["Pantone", "PantoneId", "Pantone色号"], er_path="clothing_info.Id = clothing_images_color.ClothingId"),
    _field("图片颜色占比", "clothing_images_color", "Percent", "metric", "图片中该颜色的识别占比。", aliases=["Percent", "颜色占比", "图片颜色占比"], unit="ratio", aggregation="max/sum/avg", er_path="clothing_info.Id = clothing_images_color.ClothingId"),
    _field("图案", "clothing_pattern_info", "pattern", "dimension", "AI识别出的商品图案标签。", aliases=["pattern", "Pattern", "图案", "图案标签"], er_path="clothing_info.Id = clothing_pattern_info.ClothingId"),
    _field("肌理", "clothing_texture_info", "Texture", "dimension", "AI识别出的材质肌理标签。", aliases=["Texture", "肌理", "材质肌理"], er_path="clothing_info.Id = clothing_texture_info.ClothingId"),
    _field("织造方式", "clothing_texture_info", "FabricType", "dimension", "AI识别出的织造方式。", aliases=["FabricType", "织造方式", "组织方式"], er_path="clothing_info.Id = clothing_texture_info.ClothingId"),
    _field("图案布局", "clothing_texture_info", "PatternLayout", "dimension", "图案布局与排列方式。", aliases=["PatternLayout", "图案布局", "图案排列"], er_path="clothing_info.Id = clothing_texture_info.ClothingId"),
    _field("工艺类型", "clothing_texture_info", "PatternTechnique", "dimension", "图案或表面工艺类型。", aliases=["PatternTechnique", "工艺类型", "图案工艺"], er_path="clothing_info.Id = clothing_texture_info.ClothingId"),
    _field("图案构成", "clothing_texture_info", "PatternComposition", "dimension", "图案构成方式。", aliases=["PatternComposition", "图案构成"], er_path="clothing_info.Id = clothing_texture_info.ClothingId"),
    _field("图案定义", "clothing_texture_info", "PatternDefinition", "dimension", "图案定义类别。", aliases=["PatternDefinition", "图案定义"], er_path="clothing_info.Id = clothing_texture_info.ClothingId"),
    _field("图案风格", "clothing_texture_info", "PatternStyle", "dimension", "图案风格与效果。", aliases=["PatternStyle", "图案风格", "风格效果"], er_path="clothing_info.Id = clothing_texture_info.ClothingId"),
]

SIZE_TEXT_CANDIDATE_FIELDS = [
    _field("中文详情", "clothing_info", "DescribeInfo", "filter", "中文商品详情文本，可用于筛选尺码/尺寸抽取候选，不是结构化尺码字段。", aliases=["DescribeInfo", "中文详情", "商品详情", "详情文本"]),
    _field("外文详情", "clothing_info", "DescribeInfoEn", "filter", "外文商品详情文本，可能包含SIZE TABLE/サイズ等尺码文本，不是结构化尺码字段。", aliases=["DescribeInfoEn", "外文详情", "SIZE TABLE", "size table"]),
    _field("其他特征", "clothing_info", "OtherFeatures", "filter", "其他特征文本，可用于尺码/尺寸文本线索筛选。", aliases=["OtherFeatures", "其他特征", "尺码文本线索"]),
]

VISUAL_ANALYSIS_RELATIONS = [
    _relation("clothing_info", "Id", "clothing_images_color", "ClothingId", "LEFT", "商品到图片识别颜色，一对多关系，统计时需要COUNT DISTINCT商品ID。"),
    _relation("clothing_info", "Id", "clothing_pattern_info", "ClothingId", "LEFT", "商品到图案识别结果，一对多关系，统计时需要COUNT DISTINCT商品ID。"),
    _relation("clothing_info", "Id", "clothing_texture_info", "ClothingId", "LEFT", "商品到肌理/织造/图案工艺识别结果，一对一或一对多关系，统计时需要COUNT DISTINCT商品ID。"),
]


COMPETITOR_SCENE_ID = "scene_prd_competitor"
COMPETITOR_SCENE_NAME = "竞品与价格分析"
COMPETITOR_SCENE_DESCRIPTION = (
    "围绕品牌、品类、价格带、来源站点、材质、功能、图案和肌理做竞品对比；当前数据不具备真实平台价差和同款识别能力。"
)

COMPETITOR_SCENE_SAMPLE_GOALS = [
    "最近30天各二级类目下各品牌的价格带分布如何",
    "按二级类目分组，各品牌的价格定位差异是什么，返回SKU数、均价、价格跨度",
    "各来源站点域名下品牌SKU覆盖和平均价格差异是什么，注意这不是平台价差",
    "各二级类目中品牌SKU覆盖和平均价差异是多少，识别可对比的品牌品类组合",
    "各品牌在材质维度上的SKU覆盖和平均价格有什么差异",
    "各品牌功能标签覆盖数、SKU数和平均价格有什么差异",
    "按二级类目分组，各品牌图案和肌理结构差异是什么",
    "各品牌在织造方式和工艺类型上的覆盖差异是什么",
]

COMPETITOR_SCENE_FIELDS = [
    _field("商品ID", "clothing_info", "Id", "filter", "SKU唯一标识，用于COUNT DISTINCT和多表去重。", required=True, aliases=["Id", "SKU", "商品ID"]),
    _field("商品名称", "clothing_info", "Name", "dimension", "商品展示名称，用于下钻到具体竞品商品。", aliases=["Name", "商品名", "商品名称"]),
    _field("品牌", "clothing_info", "BrandName", "dimension", "竞品对比的核心主体。", required=True, aliases=["BrandName", "品牌"]),
    _field("一级类目", "clothing_info", "Category", "dimension", "竞品分析的主品类维度。", aliases=["Category", "一级品类", "一级类目"]),
    _field("二级类目", "clothing_info", "SubCategory", "dimension", "同品类竞品对比的主要细分维度。", required=True, aliases=["SubCategory", "二级品类", "二级类目"]),
    _field("叶子类目", "clothing_info", "LeafCategory", "dimension", "更细粒度的竞品定位维度。", aliases=["LeafCategory", "叶子类目"]),
    _field("价格", "clothing_info", "Price", "metric", "销售价/换算价主指标，适合品牌、品类、来源站点之间对比。", required=True, aliases=["Price", "价格", "售价", "销售价"], unit="price", aggregation="avg/min/max"),
    _field("原始标价", "clothing_info", "OriginalPrice", "metric", "原始站点标价字符串，存在币种信息；未做币种统一前不要直接作为折扣率分母。", aliases=["OriginalPrice", "原价", "吊牌价", "原始标价"]),
    _field("抓取日期", "clothing_info", "ReceiveTime", "time", "用于最近窗口、批次和聚合趋势。", aliases=["ReceiveTime", "抓取时间", "抓取日期"]),
    _field("来源站点域名", "clothing_info", "SourceUrl", "dimension", "从SourceUrl提取域名作为来源站点口径，不等同于销售平台。", aliases=["SourceUrl", "来源站点", "站点域名"]),
    _field("材质名称", "clothing_fiber_info", "Name", "filter", "商品材质标签，用于品牌材质覆盖对比。", aliases=["材质", "材质名称", "Fiber", "Name"], er_path="clothing_info.Id = clothing_fiber_info.ClothingId"),
    _field("功能标签", "clothing_functions_info", "Functionality", "filter", "商品功能标签，用于功能覆盖和功能价格差异。", aliases=["功能", "功能标签", "Functionality"], er_path="clothing_info.Id = clothing_functions_info.ClothingId"),
    _field("场景标签", "clothing_scene_info", "Scene", "filter", "商品适用场景标签，用于场景内竞品对比。", aliases=["Scene", "场景", "场景标签"], er_path="clothing_info.Id = clothing_scene_info.ClothingId"),
    *VISUAL_ANALYSIS_FIELDS,
]

COMPETITOR_SCENE_RELATIONS = [
    _relation("clothing_info", "Id", "clothing_fiber_info", "ClothingId", "LEFT", "商品到材质，多值关系，统计时需要COUNT DISTINCT商品ID。"),
    _relation("clothing_info", "Id", "clothing_functions_info", "ClothingId", "LEFT", "商品到功能标签，多值关系，统计时需要COUNT DISTINCT商品ID。"),
    _relation("clothing_info", "Id", "clothing_scene_info", "ClothingId", "LEFT", "商品到场景标签，多值关系，统计时需要COUNT DISTINCT商品ID。"),
    *VISUAL_ANALYSIS_RELATIONS,
]

COMPETITOR_SCENE_QUESTION_MATRIX = [
    {
        "preset_key": "competitor_recent_category_price_band",
        "title": "品牌品类价格带分布",
        "question": "最近30天各二级类目下各品牌的价格带分布如何",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "SKU数去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "竞品品牌分组"),
            _requirement("二级类目", "clothing_info", "SubCategory", "dimension", "同品类竞品分组范围"),
            _requirement("价格", "clothing_info", "Price", "metric", "价格带分桶依据"),
            _requirement("抓取日期", "clothing_info", "ReceiveTime", "time", "最近30天窗口"),
        ],
        "derived_metrics": [
            {"name": "价格带SKU数", "formula": "COUNT(DISTINCT Id) by fixed price band"},
            {"name": "品牌内价格带占比", "formula": "价格带SKU数 / 品牌SKU数"},
        ],
        "group_by": ["品牌", "二级类目", "价格带"],
        "sort": [{"metric": "二级类目", "direction": "ASC"}, {"metric": "品牌", "direction": "ASC"}, {"metric": "价格带", "direction": "ASC"}],
        "limit": None,
        "notes": ["符合PRD 8.1的品牌价格带分布", "价格带口径固定，不由模型自由改桶宽", "没有具体二级类目参数时按二级类目分组，禁止生成 :subcategory 占位符", "最近30天以数据最大 ReceiveTime 为锚点"],
    },
    {
        "preset_key": "competitor_subcategory_price_position",
        "title": "同品类品牌价格定位",
        "question": "按二级类目分组，各品牌的价格定位差异是什么，返回SKU数、均价、价格跨度",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "SKU数去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "竞品品牌分组"),
            _requirement("二级类目", "clothing_info", "SubCategory", "dimension", "同品类对比范围"),
            _requirement("价格", "clothing_info", "Price", "metric", "均价、最高价、最低价和价差"),
        ],
        "derived_metrics": [
            {"name": "SKU数", "formula": "COUNT(DISTINCT Id)"},
            {"name": "平均价", "formula": "AVG(Price)"},
            {"name": "价格跨度", "formula": "MAX(Price) - MIN(Price)"},
        ],
        "group_by": ["二级类目", "品牌"],
        "sort": [{"metric": "平均价", "direction": "DESC"}],
        "limit": 30,
        "notes": ["用于替代自由文本里容易误写的平台竞品逻辑", "没有具体二级类目参数时按二级类目分组，禁止生成占位符"],
    },
    {
        "preset_key": "competitor_source_site_brand_price",
        "title": "来源站点品牌价格对比",
        "question": "各来源站点域名下品牌SKU覆盖和平均价格差异是什么，注意这不是平台价差",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "SKU数去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌覆盖统计"),
            _requirement("价格", "clothing_info", "Price", "metric", "均价统计"),
            _requirement("来源站点域名", "clothing_info", "SourceUrl", "dimension", "来源站点分组口径"),
        ],
        "derived_metrics": [
            {"name": "品牌覆盖数", "formula": "COUNT(DISTINCT BrandName)"},
            {"name": "SKU数", "formula": "COUNT(DISTINCT Id)"},
            {"name": "平均价", "formula": "AVG(Price)"},
        ],
        "group_by": ["来源站点域名", "品牌"],
        "sort": [{"metric": "SKU数", "direction": "DESC"}],
        "limit": 50,
        "notes": ["SourceUrl只能支撑来源站点分析，不等价于销售平台分析"],
    },
    {
        "preset_key": "competitor_two_brand_overlap",
        "title": "品牌品类覆盖对比",
        "question": "各二级类目中品牌SKU覆盖和平均价差异是多少，识别可对比的品牌品类组合",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "SKU数去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "竞品品牌分组"),
            _requirement("二级类目", "clothing_info", "SubCategory", "dimension", "重叠品类识别"),
            _requirement("价格", "clothing_info", "Price", "metric", "品牌均价和价差"),
        ],
        "derived_metrics": [
            {"name": "品牌SKU数", "formula": "COUNT(DISTINCT Id)"},
            {"name": "品牌均价", "formula": "AVG(Price)"},
            {"name": "品类内品牌均价差", "formula": "同一二级类目下品牌平均价与品类均价的差异，或同类目品牌均价范围"},
        ],
        "group_by": ["二级类目", "品牌"],
        "sort": [{"metric": "二级类目", "direction": "ASC"}, {"metric": "品牌SKU数", "direction": "DESC"}],
        "limit": 30,
        "notes": ["不依赖同款识别；按品牌/品类/价格带做第一阶段竞品分析", "没有指定品牌参数时不要生成 :brand 占位符"],
    },
    {
        "preset_key": "competitor_fiber_coverage_price",
        "title": "品牌材质覆盖对比",
        "question": "各品牌在材质维度上的SKU覆盖和平均价格有什么差异",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "多值材质关联下的去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌分组"),
            _requirement("价格", "clothing_info", "Price", "metric", "材质下均价"),
            _requirement("材质名称", "clothing_fiber_info", "Name", "filter", "材质覆盖维度"),
        ],
        "derived_metrics": [
            {"name": "材质覆盖数", "formula": "COUNT(DISTINCT clothing_fiber_info.Name)"},
            {"name": "SKU数", "formula": "COUNT(DISTINCT clothing_info.Id)"},
            {"name": "平均价", "formula": "AVG(clothing_info.Price)"},
        ],
        "group_by": ["品牌", "材质名称"],
        "sort": [{"metric": "SKU数", "direction": "DESC"}],
        "limit": 50,
        "notes": ["材质表是一对多关系，必须COUNT DISTINCT商品ID"],
    },
    {
        "preset_key": "competitor_function_coverage_price",
        "title": "品牌功能覆盖对比",
        "question": "各品牌功能标签覆盖数、SKU数和平均价格有什么差异",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "多值功能关联下的去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌分组"),
            _requirement("价格", "clothing_info", "Price", "metric", "功能下均价"),
            _requirement("功能标签", "clothing_functions_info", "Functionality", "filter", "功能覆盖维度"),
        ],
        "derived_metrics": [
            {"name": "功能覆盖数", "formula": "COUNT(DISTINCT clothing_functions_info.Functionality)"},
            {"name": "SKU数", "formula": "COUNT(DISTINCT clothing_info.Id)"},
            {"name": "平均价", "formula": "AVG(clothing_info.Price)"},
        ],
        "group_by": ["品牌", "功能标签"],
        "sort": [{"metric": "SKU数", "direction": "DESC"}],
        "limit": 50,
        "notes": ["功能表是一对多关系，必须COUNT DISTINCT商品ID"],
    },
    {
        "preset_key": "competitor_pattern_texture_diff",
        "title": "竞品图案肌理差异",
        "question": "按二级类目分组，各品牌图案和肌理结构差异是什么",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "多表关联下的去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌分组"),
            _requirement("二级类目", "clothing_info", "SubCategory", "dimension", "同品类竞品范围"),
            _requirement("图案", "clothing_pattern_info", "pattern", "dimension", "图案结构维度"),
            _requirement("肌理", "clothing_texture_info", "Texture", "dimension", "肌理结构维度"),
        ],
        "derived_metrics": [
            {"name": "SKU数", "formula": "COUNT(DISTINCT clothing_info.Id)"},
            {"name": "图案覆盖数", "formula": "COUNT(DISTINCT clothing_pattern_info.pattern)"},
            {"name": "肌理覆盖数", "formula": "COUNT(DISTINCT clothing_texture_info.Texture)"},
        ],
        "group_by": ["二级类目", "品牌", "图案", "肌理"],
        "sort": [{"metric": "SKU数", "direction": "DESC"}],
        "limit": 50,
        "notes": ["图案和肌理表都是AI识别结果，多表关联时必须COUNT DISTINCT商品ID"],
    },
    {
        "preset_key": "competitor_fabric_technique_coverage",
        "title": "竞品织造工艺覆盖",
        "question": "各品牌在织造方式和工艺类型上的覆盖差异是什么",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "多表关联下的去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌分组"),
            _requirement("织造方式", "clothing_texture_info", "FabricType", "dimension", "织造方式覆盖"),
            _requirement("工艺类型", "clothing_texture_info", "PatternTechnique", "dimension", "图案/表面工艺覆盖"),
        ],
        "derived_metrics": [
            {"name": "SKU数", "formula": "COUNT(DISTINCT clothing_info.Id)"},
            {"name": "织造方式覆盖数", "formula": "COUNT(DISTINCT clothing_texture_info.FabricType)"},
            {"name": "工艺类型覆盖数", "formula": "COUNT(DISTINCT clothing_texture_info.PatternTechnique)"},
        ],
        "group_by": ["品牌", "织造方式", "工艺类型"],
        "sort": [{"metric": "SKU数", "direction": "DESC"}],
        "limit": 50,
        "notes": ["用于补充PRD竞品分析中的商品结构和设计元素对比"],
    },
]

COMPETITOR_SCENE_OUT_OF_SCOPE = [
    "真实平台销售价差",
    "依赖同款识别的商品级竞品匹配",
    "未做币种归一前的严格折扣力度",
    "同一SKU跨日期历史价格变化",
]


STRUCTURE_SCENE_ID = "scene_prd_structure"
STRUCTURE_SCENE_NAME = "商品结构分析"
STRUCTURE_SCENE_DESCRIPTION = "围绕品牌、品类、颜色、图片主色、图案、肌理、织造方式、工艺、价格带和上新做SKU结构与布局分析；当前数据不支持结构化尺码。"

STRUCTURE_SCENE_SAMPLE_GOALS = [
    "各品牌的一级类目和二级类目布局分别是什么，返回SKU数和品牌内占比",
    "各品牌SKU丰富度排行，返回品牌、SKU数、覆盖二级类目数、覆盖叶子类目数",
    "各二级类目中品牌覆盖数和SKU数是多少，识别竞争最充分的品类",
    "各品牌颜色丰富度排行，返回颜色数、SKU数、主力颜色",
    "各品牌图片主色和Pantone色号覆盖结构是什么",
    "各品牌图案、肌理、织造方式和工艺类型结构是什么",
    "最近上新商品在品牌和二级类目上的结构是什么",
    "各价格带中的品类结构是什么，返回价格带、一级类目、SKU数、占比",
    "哪些商品描述中包含尺码、尺寸或SIZE TABLE，可作为尺码抽取候选",
]

STRUCTURE_SCENE_FIELDS = [
    _field("商品ID", "clothing_info", "Id", "filter", "SKU唯一标识，用于结构统计去重。", required=True, aliases=["Id", "SKU", "商品ID"]),
    _field("商品名称", "clothing_info", "Name", "dimension", "商品下钻展示名称。", aliases=["Name", "商品名", "商品名称"]),
    _field("品牌", "clothing_info", "BrandName", "dimension", "品牌结构分析维度。", required=True, aliases=["BrandName", "品牌"]),
    _field("一级类目", "clothing_info", "Category", "dimension", "商品结构的主品类维度。", required=True, aliases=["Category", "一级类目", "一级品类"]),
    _field("二级类目", "clothing_info", "SubCategory", "dimension", "商品结构的细分品类维度。", required=True, aliases=["SubCategory", "二级类目", "二级品类"]),
    _field("叶子类目", "clothing_info", "LeafCategory", "dimension", "SKU丰富度和品类深度维度。", aliases=["LeafCategory", "叶子类目"]),
    _field("颜色", "clothing_info", "ColorName", "dimension", "颜色结构分析维度。", aliases=["ColorName", "颜色", "色彩"]),
    _field("价格", "clothing_info", "Price", "metric", "结构视角下的价格带分桶和均价指标。", aliases=["Price", "价格", "售价"], unit="price", aggregation="avg/min/max"),
    _field("上架时间", "clothing_info", "CreateTime", "time", "上新结构的时间口径。", aliases=["CreateTime", "上架时间", "上新时间"]),
    _field("抓取日期", "clothing_info", "ReceiveTime", "time", "批次结构和近期窗口口径。", aliases=["ReceiveTime", "抓取日期", "抓取时间"]),
    _field("来源站点域名", "clothing_info", "SourceUrl", "dimension", "来源站点布局维度，不等同于销售平台。", aliases=["SourceUrl", "来源站点", "站点域名"]),
    _field("适用性别", "clothing_info", "SuitableGender", "filter", "女装/男装等适用人群筛选。", aliases=["SuitableGender", "性别", "适用性别"]),
    _field("场景标签", "clothing_scene_info", "Scene", "filter", "场景结构分析维度。", aliases=["Scene", "场景", "场景标签"], er_path="clothing_info.Id = clothing_scene_info.ClothingId"),
    *VISUAL_ANALYSIS_FIELDS,
    *SIZE_TEXT_CANDIDATE_FIELDS,
]

STRUCTURE_SCENE_RELATIONS = [
    _relation("clothing_info", "Id", "clothing_scene_info", "ClothingId", "LEFT", "商品到场景标签，多值关系，统计时需要COUNT DISTINCT商品ID。"),
    *VISUAL_ANALYSIS_RELATIONS,
]

STRUCTURE_SCENE_QUESTION_MATRIX = [
    {
        "preset_key": "structure_brand_category_layout",
        "title": "品牌品类布局",
        "question": "各品牌的一级类目和二级类目布局分别是什么，返回SKU数和品牌内占比",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "SKU数去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌分组"),
            _requirement("一级类目", "clothing_info", "Category", "dimension", "一级结构分组"),
            _requirement("二级类目", "clothing_info", "SubCategory", "dimension", "二级结构分组"),
        ],
        "derived_metrics": [
            {"name": "SKU数", "formula": "COUNT(DISTINCT Id)"},
            {"name": "品牌内占比", "formula": "SKU数 / 品牌总SKU数"},
        ],
        "group_by": ["品牌", "一级类目", "二级类目"],
        "sort": [{"metric": "SKU数", "direction": "DESC"}],
        "limit": 50,
        "notes": ["符合PRD 8.2的品类结构和品牌布局分析"],
    },
    {
        "preset_key": "structure_brand_sku_richness",
        "title": "品牌SKU丰富度",
        "question": "各品牌SKU丰富度排行，返回品牌、SKU数、覆盖二级类目数、覆盖叶子类目数",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "SKU数去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌分组"),
            _requirement("二级类目", "clothing_info", "SubCategory", "dimension", "覆盖宽度"),
            _requirement("叶子类目", "clothing_info", "LeafCategory", "dimension", "覆盖深度"),
        ],
        "derived_metrics": [
            {"name": "SKU数", "formula": "COUNT(DISTINCT Id)"},
            {"name": "覆盖二级类目数", "formula": "COUNT(DISTINCT SubCategory)"},
            {"name": "覆盖叶子类目数", "formula": "COUNT(DISTINCT LeafCategory)"},
        ],
        "group_by": ["品牌"],
        "sort": [{"metric": "SKU数", "direction": "DESC"}],
        "limit": 20,
        "notes": ["直接回答SKU丰富度"],
    },
    {
        "preset_key": "structure_subcategory_competition",
        "title": "品类竞争充分度",
        "question": "各二级类目中品牌覆盖数和SKU数是多少，识别竞争最充分的品类",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "SKU数去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌覆盖统计"),
            _requirement("二级类目", "clothing_info", "SubCategory", "dimension", "品类分组"),
        ],
        "derived_metrics": [
            {"name": "品牌覆盖数", "formula": "COUNT(DISTINCT BrandName)"},
            {"name": "SKU数", "formula": "COUNT(DISTINCT Id)"},
        ],
        "group_by": ["二级类目"],
        "sort": [{"metric": "品牌覆盖数", "direction": "DESC"}, {"metric": "SKU数", "direction": "DESC"}],
        "limit": 30,
        "notes": ["可用于识别布局集中或竞争充分的品类"],
    },
    {
        "preset_key": "structure_brand_color_richness",
        "title": "品牌颜色结构",
        "question": "各品牌颜色丰富度排行，返回颜色数、SKU数、主力颜色",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "SKU数去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌分组"),
            _requirement("颜色", "clothing_info", "ColorName", "dimension", "颜色结构维度"),
        ],
        "derived_metrics": [
            {"name": "颜色数", "formula": "COUNT(DISTINCT ColorName)"},
            {"name": "SKU数", "formula": "COUNT(DISTINCT Id)"},
            {"name": "主力颜色", "formula": "品牌内SKU数最高的ColorName"},
        ],
        "group_by": ["品牌", "颜色"],
        "sort": [{"metric": "SKU数", "direction": "DESC"}],
        "limit": 50,
        "notes": ["当前数据有颜色字段，但没有尺码字段"],
    },
    {
        "preset_key": "structure_image_color_coverage",
        "title": "图片主色结构",
        "question": "各品牌图片主色和Pantone色号覆盖结构是什么",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "图片颜色多值关联下的去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌分组"),
            _requirement("Pantone色号", "clothing_images_color", "PantoneId", "dimension", "图片识别色号"),
            _requirement("图片颜色占比", "clothing_images_color", "Percent", "metric", "主色判定依据"),
        ],
        "derived_metrics": [
            {"name": "SKU数", "formula": "COUNT(DISTINCT clothing_info.Id)"},
            {"name": "Pantone覆盖数", "formula": "COUNT(DISTINCT clothing_images_color.PantoneId)"},
            {"name": "平均颜色占比", "formula": "AVG(clothing_images_color.Percent)"},
        ],
        "group_by": ["品牌", "Pantone色号"],
        "sort": [{"metric": "SKU数", "direction": "DESC"}, {"metric": "平均颜色占比", "direction": "DESC"}],
        "limit": 50,
        "notes": [
            "图片颜色表是一对多关系，统计时必须COUNT DISTINCT商品ID",
            "主色必须按每个 ClothingId 的 Percent 最大记录判定，并过滤 PantoneId/RGB 为空的颜色记录",
        ],
    },
    {
        "preset_key": "structure_pattern_texture_mix",
        "title": "图案肌理工艺结构",
        "question": "各品牌图案、肌理、织造方式和工艺类型结构是什么",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "多表关联下的去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌分组"),
            _requirement("图案", "clothing_pattern_info", "pattern", "dimension", "图案结构"),
            _requirement("肌理", "clothing_texture_info", "Texture", "dimension", "肌理结构"),
            _requirement("织造方式", "clothing_texture_info", "FabricType", "dimension", "织造方式结构"),
            _requirement("工艺类型", "clothing_texture_info", "PatternTechnique", "dimension", "工艺类型结构"),
        ],
        "derived_metrics": [
            {"name": "SKU数", "formula": "COUNT(DISTINCT clothing_info.Id)"},
            {"name": "图案覆盖数", "formula": "COUNT(DISTINCT clothing_pattern_info.pattern)"},
            {"name": "肌理覆盖数", "formula": "COUNT(DISTINCT clothing_texture_info.Texture)"},
            {"name": "工艺类型覆盖数", "formula": "COUNT(DISTINCT clothing_texture_info.PatternTechnique)"},
        ],
        "group_by": ["品牌", "图案", "肌理", "织造方式", "工艺类型"],
        "sort": [{"metric": "SKU数", "direction": "DESC"}],
        "limit": 50,
        "notes": ["补充PRD商品结构分析中的颜色/款式/材质视觉结构"],
    },
    {
        "preset_key": "structure_recent_new_arrival_mix",
        "title": "近期上新结构",
        "question": "最近上新商品在品牌和二级类目上的结构是什么",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "SKU数去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌分组"),
            _requirement("二级类目", "clothing_info", "SubCategory", "dimension", "上新品类结构"),
            _requirement("上架时间", "clothing_info", "CreateTime", "time", "上新窗口"),
        ],
        "derived_metrics": [
            {"name": "新增SKU数", "formula": "COUNT(DISTINCT Id)"},
            {"name": "上新占比", "formula": "新增SKU数 / 窗口内总新增SKU数"},
        ],
        "group_by": ["品牌", "二级类目", "上架日期"],
        "sort": [{"metric": "新增SKU数", "direction": "DESC"}],
        "limit": 50,
        "notes": ["符合PRD 8.2的上新结构", "最近上新以数据最大 CreateTime 为锚点，不使用系统当前日期"],
    },
    {
        "preset_key": "structure_price_band_category_mix",
        "title": "价格带品类结构",
        "question": "各价格带中的品类结构是什么，返回价格带、一级类目、SKU数、占比",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "SKU数去重基准"),
            _requirement("一级类目", "clothing_info", "Category", "dimension", "品类结构"),
            _requirement("价格", "clothing_info", "Price", "metric", "价格带分桶依据"),
        ],
        "derived_metrics": [
            {"name": "SKU数", "formula": "COUNT(DISTINCT Id)"},
            {"name": "价格带内占比", "formula": "SKU数 / 价格带总SKU数"},
        ],
        "group_by": ["价格带", "一级类目"],
        "sort": [{"metric": "价格带", "direction": "ASC"}, {"metric": "SKU数", "direction": "DESC"}],
        "limit": 50,
        "notes": ["把价格结构纳入商品结构视角，不需要平台字段"],
    },
    {
        "preset_key": "structure_size_text_candidates",
        "title": "尺码文本抽取候选",
        "question": "哪些商品描述中包含尺码、尺寸或SIZE TABLE，可作为尺码抽取候选",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "候选商品唯一标识"),
            _requirement("商品名称", "clothing_info", "Name", "dimension", "候选商品展示"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌展示"),
            _requirement("一级类目", "clothing_info", "Category", "dimension", "类目展示"),
            _requirement("中文详情", "clothing_info", "DescribeInfo", "filter", "尺码/尺寸中文文本线索"),
            _requirement("外文详情", "clothing_info", "DescribeInfoEn", "filter", "SIZE TABLE/サイズ文本线索"),
            _requirement("其他特征", "clothing_info", "OtherFeatures", "filter", "其他尺码文本线索", required=False),
        ],
        "derived_metrics": [
            {
                "name": "命中线索",
                "formula": "CASE WHEN 描述命中 SIZE TABLE/サイズ/尺码/size/尺寸 THEN 返回对应线索；这是商品明细清单，不使用 COUNT 聚合",
            },
        ],
        "group_by": ["商品ID", "商品名称", "品牌", "一级类目"],
        "sort": [{"metric": "品牌", "direction": "ASC"}],
        "limit": 50,
        "notes": [
            "这是文本抽取候选清单，不是结构化尺码分布",
            "不能据此直接生成尺码结构结论",
            "必须返回商品级明细：商品ID、商品名称、品牌、一级类目、命中线索；不要使用 COUNT、GROUP BY 或聚合指标",
            "优先匹配 SIZE TABLE、サイズ、尺码；泛化的“尺寸”只能作为低优先级线索，避免把非服装商品尺寸当尺码",
        ],
    },
]

STRUCTURE_SCENE_OUT_OF_SCOPE = [
    "尺码结构",
    "真实平台之间的商品布局差异",
    "库存结构",
    "销售贡献结构",
]


TREND_SCENE_ID = "scene_prd_trend"
TREND_SCENE_NAME = "趋势与爆款分析"
TREND_SCENE_DESCRIPTION = (
    "围绕上新节奏、SKU数量变化、聚合价格波动、视觉元素变化、异常变化和潜在重点商品做趋势分析；当前数据不支持真实销量爆款。"
)

TREND_SCENE_SAMPLE_GOALS = [
    "按上架日期统计每日新增SKU数，识别上新高峰日期",
    "各品牌每日上新SKU数变化趋势是什么",
    "最近一次抓取批次中，各品牌新增商品数量排行",
    "各品类在抓取日期上的SKU数变化是什么",
    "潜在高价值新品：最近上架且价格高于全量均价2倍的商品有哪些",
    "各场景标签最近上新SKU数和平均价格变化是什么",
    "最近上新商品的图案、肌理和主色趋势是什么",
    "潜在高价值新品的图案、肌理、Pantone色号和工艺特征是什么",
]

TREND_SCENE_FIELDS = [
    _field("商品ID", "clothing_info", "Id", "filter", "SKU唯一标识，用于趋势统计去重。", required=True, aliases=["Id", "SKU", "商品ID"]),
    _field("商品名称", "clothing_info", "Name", "dimension", "潜在重点商品下钻展示。", aliases=["Name", "商品名", "商品名称"]),
    _field("商品源ID", "clothing_info", "ProductId", "filter", "来源商品ID；当前数据没有跨日期重复快照，不能支撑同一商品价格历史。", aliases=["ProductId", "商品源ID", "源商品ID"]),
    _field("品牌", "clothing_info", "BrandName", "dimension", "品牌趋势分组。", required=True, aliases=["BrandName", "品牌"]),
    _field("一级类目", "clothing_info", "Category", "dimension", "品类趋势分组。", required=True, aliases=["Category", "一级类目", "一级品类"]),
    _field("二级类目", "clothing_info", "SubCategory", "dimension", "细分品类趋势分组。", aliases=["SubCategory", "二级类目", "二级品类"]),
    _field("价格", "clothing_info", "Price", "metric", "聚合价格趋势和高价值商品识别指标。", required=True, aliases=["Price", "价格", "售价"], unit="price", aggregation="avg/min/max"),
    _field("上架时间", "clothing_info", "CreateTime", "time", "上新节奏的主时间口径。", required=True, aliases=["CreateTime", "上架时间", "上新时间"]),
    _field("抓取日期", "clothing_info", "ReceiveTime", "time", "批次趋势和近期窗口口径。", required=True, aliases=["ReceiveTime", "抓取日期", "抓取时间"]),
    _field("来源站点域名", "clothing_info", "SourceUrl", "dimension", "来源站点趋势维度，不等同于销售平台。", aliases=["SourceUrl", "来源站点", "站点域名"]),
    _field("颜色", "clothing_info", "ColorName", "dimension", "趋势下钻维度。", aliases=["ColorName", "颜色"]),
    _field("功能标签", "clothing_functions_info", "Functionality", "filter", "潜在重点商品和功能趋势辅助维度。", aliases=["功能", "功能标签", "Functionality"], er_path="clothing_info.Id = clothing_functions_info.ClothingId"),
    _field("场景标签", "clothing_scene_info", "Scene", "filter", "场景趋势分析维度。", aliases=["Scene", "场景", "场景标签"], er_path="clothing_info.Id = clothing_scene_info.ClothingId"),
    *VISUAL_ANALYSIS_FIELDS,
]

TREND_SCENE_RELATIONS = [
    _relation("clothing_info", "Id", "clothing_functions_info", "ClothingId", "LEFT", "商品到功能标签，多值关系，统计时需要COUNT DISTINCT商品ID。"),
    _relation("clothing_info", "Id", "clothing_scene_info", "ClothingId", "LEFT", "商品到场景标签，多值关系，统计时需要COUNT DISTINCT商品ID。"),
    *VISUAL_ANALYSIS_RELATIONS,
]

TREND_SCENE_QUESTION_MATRIX = [
    {
        "preset_key": "trend_daily_new_sku",
        "title": "每日上新节奏",
        "question": "按上架日期统计每日新增SKU数，识别上新高峰日期",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "新增SKU去重基准"),
            _requirement("上架时间", "clothing_info", "CreateTime", "time", "每日上新时间口径"),
        ],
        "derived_metrics": [
            {"name": "新增SKU数", "formula": "COUNT(DISTINCT Id)"},
            {"name": "较前一日变化", "formula": "新增SKU数 - LAG(新增SKU数)"},
        ],
        "group_by": ["上架日期"],
        "sort": [{"metric": "上架日期", "direction": "ASC"}],
        "limit": None,
        "notes": ["符合PRD 8.3的上新节奏"],
    },
    {
        "preset_key": "trend_brand_daily_new_sku",
        "title": "品牌上新趋势",
        "question": "各品牌每日上新SKU数变化趋势是什么",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "新增SKU去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌分组"),
            _requirement("上架时间", "clothing_info", "CreateTime", "time", "每日上新时间口径"),
        ],
        "derived_metrics": [
            {"name": "新增SKU数", "formula": "COUNT(DISTINCT Id)"},
            {"name": "品牌日环比变化", "formula": "新增SKU数 - LAG(新增SKU数) OVER(PARTITION BY BrandName)"},
        ],
        "group_by": ["品牌", "上架日期"],
        "sort": [{"metric": "上架日期", "direction": "ASC"}, {"metric": "新增SKU数", "direction": "DESC"}],
        "limit": None,
        "notes": ["用于回答哪些品牌上新节奏明显加快"],
    },
    {
        "preset_key": "trend_latest_batch_brand_new_rank",
        "title": "最近批次品牌上新排行",
        "question": "最近一次抓取批次中，各品牌新增商品数量排行",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "SKU数去重基准"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌分组"),
            _requirement("抓取日期", "clothing_info", "ReceiveTime", "time", "最近抓取批次"),
        ],
        "derived_metrics": [
            {"name": "批次SKU数", "formula": "COUNT(DISTINCT Id) in latest ReceiveTime date"},
        ],
        "group_by": ["品牌"],
        "sort": [{"metric": "批次SKU数", "direction": "DESC"}],
        "limit": 20,
        "notes": ["当前以最近抓取批次近似近期上新批次", "最近抓取批次使用 MAX(DATE(ReceiveTime))"],
    },
    {
        "preset_key": "trend_category_receive_change",
        "title": "品类SKU数量变化",
        "question": "各品类在抓取日期上的SKU数变化是什么",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "SKU数去重基准"),
            _requirement("一级类目", "clothing_info", "Category", "dimension", "品类分组"),
            _requirement("抓取日期", "clothing_info", "ReceiveTime", "time", "批次趋势时间口径"),
        ],
        "derived_metrics": [
            {"name": "SKU数", "formula": "COUNT(DISTINCT Id)"},
            {"name": "较前批次变化", "formula": "SKU数 - LAG(SKU数) OVER(PARTITION BY Category)"},
        ],
        "group_by": ["一级类目", "抓取日期"],
        "sort": [{"metric": "抓取日期", "direction": "ASC"}],
        "limit": None,
        "notes": ["用于识别近30天品类异常变化，但异常阈值需业务配置"],
    },
    {
        "preset_key": "trend_high_value_new_items",
        "title": "潜在高价值新品",
        "question": "潜在高价值新品：最近上架且价格高于全量均价2倍的商品有哪些",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "商品唯一标识"),
            _requirement("商品名称", "clothing_info", "Name", "dimension", "商品展示"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌展示"),
            _requirement("一级类目", "clothing_info", "Category", "dimension", "品类展示"),
            _requirement("价格", "clothing_info", "Price", "metric", "高价值阈值"),
            _requirement("上架时间", "clothing_info", "CreateTime", "time", "最近上架窗口"),
        ],
        "derived_metrics": [
            {"name": "全量均价", "formula": "AVG(Price) over all products"},
            {"name": "高价值倍数", "formula": "Price / 全量均价"},
        ],
        "group_by": ["商品ID", "商品名称", "品牌", "一级类目", "上架时间"],
        "sort": [{"metric": "价格", "direction": "DESC"}],
        "limit": 30,
        "notes": ["这是潜在重点商品代理指标，不等同于真实爆款", "最近上架以数据最大 CreateTime 为锚点"],
    },
    {
        "preset_key": "trend_scene_new_price_change",
        "title": "场景上新与均价变化",
        "question": "各场景标签最近上新SKU数和平均价格变化是什么",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "多值场景关联下的去重基准"),
            _requirement("价格", "clothing_info", "Price", "metric", "场景均价"),
            _requirement("上架时间", "clothing_info", "CreateTime", "time", "上新时间口径"),
            _requirement("场景标签", "clothing_scene_info", "Scene", "filter", "场景趋势分组"),
        ],
        "derived_metrics": [
            {"name": "新增SKU数", "formula": "COUNT(DISTINCT clothing_info.Id)"},
            {"name": "平均价", "formula": "AVG(clothing_info.Price)"},
        ],
        "group_by": ["场景标签", "上架日期"],
        "sort": [{"metric": "上架日期", "direction": "ASC"}, {"metric": "新增SKU数", "direction": "DESC"}],
        "limit": None,
        "notes": ["场景表是一对多关系，必须COUNT DISTINCT商品ID", "最近上新以数据最大 CreateTime 为锚点"],
    },
    {
        "preset_key": "trend_new_visual_element_mix",
        "title": "上新视觉元素趋势",
        "question": "最近上新商品的图案、肌理和主色趋势是什么",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "多表关联下的去重基准"),
            _requirement("上架时间", "clothing_info", "CreateTime", "time", "上新时间口径"),
            _requirement("图案", "clothing_pattern_info", "pattern", "dimension", "图案趋势维度"),
            _requirement("肌理", "clothing_texture_info", "Texture", "dimension", "肌理趋势维度"),
            _requirement("Pantone色号", "clothing_images_color", "PantoneId", "dimension", "图片主色趋势维度"),
            _requirement("图片颜色占比", "clothing_images_color", "Percent", "metric", "主色占比依据"),
        ],
        "derived_metrics": [
            {"name": "新增SKU数", "formula": "COUNT(DISTINCT clothing_info.Id)"},
            {"name": "平均主色占比", "formula": "AVG(clothing_images_color.Percent)"},
        ],
        "group_by": ["上架日期", "图案", "肌理", "Pantone色号"],
        "sort": [{"metric": "上架日期", "direction": "ASC"}, {"metric": "新增SKU数", "direction": "DESC"}],
        "limit": 80,
        "notes": [
            "图片颜色、图案和肌理均为AI识别结果，多表关联时必须COUNT DISTINCT商品ID",
            "最近上新以数据最大 CreateTime 为锚点",
            "主色必须按每个 ClothingId 的 Percent 最大记录判定，并过滤 PantoneId/RGB 为空的颜色记录",
        ],
    },
    {
        "preset_key": "trend_high_value_visual_features",
        "title": "高价值新品视觉特征",
        "question": "潜在高价值新品的图案、肌理、Pantone色号和工艺特征是什么",
        "editable": True,
        "field_requirements": [
            _requirement("商品ID", "clothing_info", "Id", "filter", "商品唯一标识"),
            _requirement("商品名称", "clothing_info", "Name", "dimension", "商品展示"),
            _requirement("品牌", "clothing_info", "BrandName", "dimension", "品牌展示"),
            _requirement("价格", "clothing_info", "Price", "metric", "高价值阈值"),
            _requirement("上架时间", "clothing_info", "CreateTime", "time", "最近上架窗口"),
            _requirement("图案", "clothing_pattern_info", "pattern", "dimension", "图案特征"),
            _requirement("肌理", "clothing_texture_info", "Texture", "dimension", "肌理特征"),
            _requirement("Pantone色号", "clothing_images_color", "PantoneId", "dimension", "图片主色特征"),
            _requirement("工艺类型", "clothing_texture_info", "PatternTechnique", "dimension", "工艺特征"),
        ],
        "derived_metrics": [
            {"name": "全量均价", "formula": "AVG(Price) over all products"},
            {"name": "高价值倍数", "formula": "Price / 全量均价"},
        ],
        "group_by": ["商品ID", "商品名称", "品牌", "图案", "肌理", "Pantone色号", "工艺类型"],
        "sort": [{"metric": "价格", "direction": "DESC"}],
        "limit": 50,
        "notes": [
            "这是高价值新品代理分析，不等同于真实销量爆款",
            "最近上架以数据最大 CreateTime 为锚点",
            "主色必须按每个 ClothingId 的 Percent 最大记录判定，并过滤 PantoneId/RGB 为空的颜色记录",
        ],
    },
]

TREND_SCENE_OUT_OF_SCOPE = [
    "真实爆款识别",
    "销量、转化率、库存驱动的趋势判断",
    "同一SKU跨日期价格波动",
    "没有业务阈值定义的自动异常归因",
]


PRICE_SCENE_CONFIG = {
    "scene_id": PRICE_SCENE_ID,
    "name": PRICE_SCENE_NAME,
    "description": PRICE_SCENE_DESCRIPTION,
    "scope": "price-question-config",
    "sample_goals": PRICE_SCENE_SAMPLE_GOALS,
    "fields": PRICE_SCENE_FIELDS,
    "relations": PRICE_SCENE_RELATIONS,
    "metric_templates": PRICE_SCENE_METRIC_TEMPLATES,
    "price_band_template": PRICE_SCENE_PRICE_BANDS,
    "question_matrix": PRICE_SCENE_QUESTION_MATRIX,
    "out_of_scope": PRICE_SCENE_OUT_OF_SCOPE,
    "notes": [
        "商品价格分析来自PRD 9.1的场景创建示例，并作为统一方案v2里的快速总览场景",
        "当前价格场景不使用平台口径",
    ],
}

COMPETITOR_SCENE_CONFIG = {
    "scene_id": COMPETITOR_SCENE_ID,
    "name": COMPETITOR_SCENE_NAME,
    "description": COMPETITOR_SCENE_DESCRIPTION,
    "scope": "competitor-question-config",
    "sample_goals": COMPETITOR_SCENE_SAMPLE_GOALS,
    "fields": COMPETITOR_SCENE_FIELDS,
    "relations": COMPETITOR_SCENE_RELATIONS,
    "metric_templates": COMMON_METRIC_TEMPLATES,
    "price_band_template": COMMON_PRICE_BANDS,
    "question_matrix": COMPETITOR_SCENE_QUESTION_MATRIX,
    "out_of_scope": COMPETITOR_SCENE_OUT_OF_SCOPE,
    "notes": [
        "对应PRD 8.1竞品与价格分析",
        "当前数据没有有效平台字段，不能输出平台价差结论；只有问题明确来源站点时才做来源站点分析",
    ],
}

STRUCTURE_SCENE_CONFIG = {
    "scene_id": STRUCTURE_SCENE_ID,
    "name": STRUCTURE_SCENE_NAME,
    "description": STRUCTURE_SCENE_DESCRIPTION,
    "scope": "structure-question-config",
    "sample_goals": STRUCTURE_SCENE_SAMPLE_GOALS,
    "fields": STRUCTURE_SCENE_FIELDS,
    "relations": STRUCTURE_SCENE_RELATIONS,
    "metric_templates": COMMON_METRIC_TEMPLATES,
    "price_band_template": COMMON_PRICE_BANDS,
    "question_matrix": STRUCTURE_SCENE_QUESTION_MATRIX,
    "out_of_scope": STRUCTURE_SCENE_OUT_OF_SCOPE,
    "notes": [
        "对应PRD 8.2商品结构分析",
        "当前数据支持品类、SKU丰富度、颜色和上新结构；不支持尺码结构",
    ],
}

TREND_SCENE_CONFIG = {
    "scene_id": TREND_SCENE_ID,
    "name": TREND_SCENE_NAME,
    "description": TREND_SCENE_DESCRIPTION,
    "scope": "trend-question-config",
    "sample_goals": TREND_SCENE_SAMPLE_GOALS,
    "fields": TREND_SCENE_FIELDS,
    "relations": TREND_SCENE_RELATIONS,
    "metric_templates": COMMON_METRIC_TEMPLATES,
    "price_band_template": COMMON_PRICE_BANDS,
    "question_matrix": TREND_SCENE_QUESTION_MATRIX,
    "out_of_scope": TREND_SCENE_OUT_OF_SCOPE,
    "notes": [
        "对应PRD 8.3趋势与爆款分析",
        "当前数据支持上新节奏、SKU数量变化和聚合均价趋势；真实爆款需要销量/库存/转化等字段",
    ],
}

PRD_SCENE_CONFIGS = [
    PRICE_SCENE_CONFIG,
    COMPETITOR_SCENE_CONFIG,
    STRUCTURE_SCENE_CONFIG,
    TREND_SCENE_CONFIG,
]

PRD_SCENE_CONFIG_BY_ID = {config["scene_id"]: config for config in PRD_SCENE_CONFIGS}
PRD_SCENE_CONFIG_BY_NAME = {config["name"]: config for config in PRD_SCENE_CONFIGS}
PRD_SCENE_NAME_ALIASES = {
    "竞品分析": COMPETITOR_SCENE_NAME,
    "上新趋势分析": TREND_SCENE_NAME,
}


def get_prd_scene_config(scene_id: str | None = None, scene_name: str | None = None) -> dict[str, Any] | None:
    scene_key = str(scene_id or "").strip()
    if scene_key in PRD_SCENE_CONFIG_BY_ID:
        return deepcopy(PRD_SCENE_CONFIG_BY_ID[scene_key])

    name_key = str(scene_name or "").strip()
    name_key = PRD_SCENE_NAME_ALIASES.get(name_key, name_key)
    if name_key in PRD_SCENE_CONFIG_BY_NAME:
        return deepcopy(PRD_SCENE_CONFIG_BY_NAME[name_key])
    return None


def build_prd_scene_templates() -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    for config in PRD_SCENE_CONFIGS:
        templates.append(
            {
                "scene_id": config["scene_id"],
                "name": config["name"],
                "description": config["description"],
                "sample_goals": list(config["sample_goals"]),
                "replace_existing": True,
                "fields": [
                    (
                        field["semantic_name"],
                        field["table_name"],
                        field["field_name"],
                        field["role"],
                        field["description"],
                    )
                    for field in config["fields"]
                ],
                "relations": [
                    (
                        relation["left_table"],
                        relation["left_field"],
                        relation["right_table"],
                        relation["right_field"],
                        relation["join_type"],
                        relation["note"],
                    )
                    for relation in config["relations"]
                ],
                "semantic_fields": deepcopy(config["fields"]),
            }
        )
    return templates
