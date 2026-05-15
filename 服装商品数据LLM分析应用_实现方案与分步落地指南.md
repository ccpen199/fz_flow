# 服装商品数据 LLM 分析应用 - 实现方案与分步落地指南

## 1. 文档目标

本文档用于把《服装商品数据 LLM 分析应用 - 产品需求文档（PRD）》落成可执行的实现方案，回答三个问题：

1. 这个系统应该怎么搭。
2. 第一阶段应该按什么顺序做。
3. 每一步分别需要做什么、产出什么、完成到什么程度才算过关。

本文档默认以 PRD 的第一阶段范围为准，不额外扩张到权限、多租户、写回数据库、爬虫、ETL、企业级模板中心等能力。

---

## 2. 先用一句话说清楚怎么实现

这个系统的实现核心不是“直接让大模型查数据库”，而是先做一层受控的业务语义层，再让 LLM 在这层语义约束下完成分析。

整体实现链路如下：

1. 接入最终业务库，只读访问商品数据。
2. 在应用侧建立场景、表、字段、ER 关系、分析指导语等配置层。
3. 用户先选择场景，再输入自然语言问题。
4. 后端把当前场景配置、字段语义、关系图、历史对话一起打包给分析 Agent。
5. Agent 生成 SQL，经过规则校验后执行只读查询。
6. 查询结果被转换成统一结构，再生成结论、图表、下一步建议。
7. 每一轮有效分析同步沉淀为一页 PPT 页面。
8. 用户继续追问时，系统沿用历史上下文和已有报告继续追加。

换句话说，真正的技术闭环是：

`业务库 -> 语义配置层 -> LLM 分析链路 -> 图表结果 -> PPT 报告`

### 2.1 第 1、2 步案例数据

下面给一套适合第一阶段 PoC 的最小案例数据，专门对应前面两步：

1. 接入最终业务库，只读访问商品数据。
2. 在应用侧建立场景、表、字段、ER 关系、分析指导语等配置层。

这套样例刻意只保留服装商品分析最常见的数据对象，方便先把链路跑通，不一开始就做得太重。

#### 第 1 步：最终业务库样例

建议先准备 4 张表就够做演示：

- `dim_platform`：平台维表
- `dim_brand`：品牌维表
- `dim_category`：品类维表
- `dwd_product_snapshot_d`：商品日快照事实表

##### `dim_platform`

```csv
platform_id,platform_code,platform_name
1,tmall,天猫
2,jd,京东
```

##### `dim_brand`

```csv
brand_id,brand_code,brand_name,brand_tier
101,zara,Zara,international
102,uniqlo,优衣库,mass
103,urban_revivo,UR,fast_fashion
```

##### `dim_category`

```csv
category_id,category_level_1,category_level_2,category_level_3
201,女装,连衣裙,长袖连衣裙
202,女装,外套,风衣
203,女装,羽绒服,短款羽绒服
204,女装,针织衫,开衫
```

##### `dwd_product_snapshot_d`

字段建议至少包含：

- `snapshot_date`：快照日期
- `item_id`：商品稳定标识
- `sku_id`：规格标识
- `platform_id`
- `brand_id`
- `category_id`
- `product_title`
- `gender_group`
- `season_tag`
- `list_price`
- `sale_price`
- `is_on_sale`
- `is_new_arrival`
- `stock_status`
- `color`
- `size`

样例数据如下：

```csv
snapshot_date,item_id,sku_id,platform_id,brand_id,category_id,product_title,gender_group,season_tag,list_price,sale_price,is_on_sale,is_new_arrival,stock_status,color,size
2026-03-01,TM_ZARA_1001,TM_ZARA_1001_BLK_S,1,101,202,Zara 女装经典中长款风衣,女装,2026春,899,699,1,1,in_stock,黑色,S
2026-03-01,TM_ZARA_1001,TM_ZARA_1001_BLK_M,1,101,202,Zara 女装经典中长款风衣,女装,2026春,899,699,1,1,in_stock,黑色,M
2026-03-01,TM_UNIQLO_2001,TM_UNIQLO_2001_BEG_M,1,102,202,优衣库轻型春季风衣,女装,2026春,599,499,1,1,in_stock,卡其,M
2026-03-01,JD_UNIQLO_2002,JD_UNIQLO_2002_WHT_M,2,102,201,优衣库针织拼接连衣裙,女装,2026春,399,329,1,0,in_stock,白色,M
2026-03-01,TM_UR_3001,TM_UR_3001_RED_S,1,103,201,UR 法式收腰连衣裙,女装,2026春,699,599,1,1,in_stock,红色,S
2026-03-01,JD_ZARA_1002,JD_ZARA_1002_GRY_M,2,101,203,Zara 短款轻羽绒夹克,女装,2025冬,1299,999,1,0,low_stock,灰色,M
2026-03-08,TM_ZARA_1001,TM_ZARA_1001_BLK_S,1,101,202,Zara 女装经典中长款风衣,女装,2026春,899,649,1,1,in_stock,黑色,S
2026-03-08,TM_ZARA_1001,TM_ZARA_1001_BLK_M,1,101,202,Zara 女装经典中长款风衣,女装,2026春,899,649,1,1,in_stock,黑色,M
2026-03-08,TM_UNIQLO_2001,TM_UNIQLO_2001_BEG_M,1,102,202,优衣库轻型春季风衣,女装,2026春,599,469,1,1,in_stock,卡其,M
2026-03-08,JD_UNIQLO_2002,JD_UNIQLO_2002_WHT_M,2,102,201,优衣库针织拼接连衣裙,女装,2026春,399,299,1,0,in_stock,白色,M
2026-03-08,TM_UR_3001,TM_UR_3001_RED_S,1,103,201,UR 法式收腰连衣裙,女装,2026春,699,569,1,1,in_stock,红色,S
2026-03-08,JD_ZARA_1002,JD_ZARA_1002_GRY_M,2,101,203,Zara 短款轻羽绒夹克,女装,2025冬,1299,899,1,0,in_stock,灰色,M
```

基于这组数据，第一阶段就已经可以支持几类真实问题：

- 最近 7 天 Zara 和优衣库的风衣价格变化如何？
- 哪个平台的连衣裙折扣更深？
- 最近一周哪些品牌女装商品降价更明显？
- 新品和非新品在售价上有什么差异？

#### 第 2 步：应用侧语义配置层样例

下面给一套“商品价格分析”场景的配置样例。第一阶段建议先只做 1 个场景，把端到端链路跑通，再复制配置扩到竞品分析、商品结构分析。

##### `analysis_scene`

```json
{
  "id": "scene_price_001",
  "scene_code": "apparel_price_analysis",
  "scene_name": "商品价格分析",
  "description": "面向服装商品价格带、折扣力度、平台差异和历史价格变化的分析场景",
  "status": "published",
  "primary_table": "dwd_product_snapshot_d",
  "default_date_field": "snapshot_date",
  "default_subject": "商品"
}
```

##### `scene_table`

```json
[
  {
    "scene_id": "scene_price_001",
    "table_name": "dwd_product_snapshot_d",
    "display_name": "商品日快照表",
    "table_role": "fact",
    "is_primary": true,
    "join_priority": 1,
    "usage_note": "用于价格、折扣、在售、上新和趋势分析"
  },
  {
    "scene_id": "scene_price_001",
    "table_name": "dim_platform",
    "display_name": "平台维表",
    "table_role": "dimension",
    "is_primary": false,
    "join_priority": 2,
    "usage_note": "用于平台对比分析"
  },
  {
    "scene_id": "scene_price_001",
    "table_name": "dim_brand",
    "display_name": "品牌维表",
    "table_role": "dimension",
    "is_primary": false,
    "join_priority": 3,
    "usage_note": "用于品牌对比分析"
  },
  {
    "scene_id": "scene_price_001",
    "table_name": "dim_category",
    "display_name": "品类维表",
    "table_role": "dimension",
    "is_primary": false,
    "join_priority": 4,
    "usage_note": "用于品类过滤和分组分析"
  }
]
```

##### `scene_field`

```json
[
  {
    "scene_id": "scene_price_001",
    "table_name": "dwd_product_snapshot_d",
    "field_name": "snapshot_date",
    "display_name": "快照日期",
    "business_desc": "商品在某一天的状态快照日期",
    "aliases": ["日期", "统计日期", "分析日期"],
    "data_type": "date",
    "is_filterable": true,
    "is_groupable": true,
    "is_aggregatable": false,
    "is_sortable": true,
    "is_time_field": true,
    "is_disabled": false
  },
  {
    "scene_id": "scene_price_001",
    "table_name": "dwd_product_snapshot_d",
    "field_name": "item_id",
    "display_name": "商品ID",
    "business_desc": "商品稳定标识，默认用于统计商品数",
    "aliases": ["商品", "款号", "item"],
    "data_type": "string",
    "is_filterable": true,
    "is_groupable": false,
    "is_aggregatable": true,
    "is_sortable": false,
    "is_time_field": false,
    "is_disabled": false
  },
  {
    "scene_id": "scene_price_001",
    "table_name": "dwd_product_snapshot_d",
    "field_name": "sale_price",
    "display_name": "销售价",
    "business_desc": "商品当前成交口径价格，优先用于价格分析",
    "aliases": ["售价", "现价", "到手价"],
    "data_type": "number",
    "is_filterable": true,
    "is_groupable": false,
    "is_aggregatable": true,
    "is_sortable": true,
    "is_time_field": false,
    "is_disabled": false
  },
  {
    "scene_id": "scene_price_001",
    "table_name": "dwd_product_snapshot_d",
    "field_name": "list_price",
    "display_name": "吊牌价",
    "business_desc": "商品原始标价，常用于计算折扣率",
    "aliases": ["原价", "牌价"],
    "data_type": "number",
    "is_filterable": true,
    "is_groupable": false,
    "is_aggregatable": true,
    "is_sortable": true,
    "is_time_field": false,
    "is_disabled": false
  },
  {
    "scene_id": "scene_price_001",
    "table_name": "dwd_product_snapshot_d",
    "field_name": "is_new_arrival",
    "display_name": "是否新品",
    "business_desc": "1 表示当前周期重点上新商品",
    "aliases": ["新品", "新上架"],
    "data_type": "boolean",
    "is_filterable": true,
    "is_groupable": true,
    "is_aggregatable": false,
    "is_sortable": false,
    "is_time_field": false,
    "is_disabled": false
  },
  {
    "scene_id": "scene_price_001",
    "table_name": "dim_platform",
    "field_name": "platform_name",
    "display_name": "平台",
    "business_desc": "商品所属电商平台",
    "aliases": ["渠道", "站点"],
    "data_type": "string",
    "is_filterable": true,
    "is_groupable": true,
    "is_aggregatable": false,
    "is_sortable": true,
    "is_time_field": false,
    "is_disabled": false
  },
  {
    "scene_id": "scene_price_001",
    "table_name": "dim_brand",
    "field_name": "brand_name",
    "display_name": "品牌",
    "business_desc": "商品所属品牌",
    "aliases": ["品牌名", "brand"],
    "data_type": "string",
    "is_filterable": true,
    "is_groupable": true,
    "is_aggregatable": false,
    "is_sortable": true,
    "is_time_field": false,
    "is_disabled": false
  },
  {
    "scene_id": "scene_price_001",
    "table_name": "dim_category",
    "field_name": "category_level_2",
    "display_name": "二级品类",
    "business_desc": "服装业务中最常用的品类分析层级",
    "aliases": ["品类", "二级类目"],
    "data_type": "string",
    "is_filterable": true,
    "is_groupable": true,
    "is_aggregatable": false,
    "is_sortable": true,
    "is_time_field": false,
    "is_disabled": false
  }
]
```

##### `scene_relation`

```json
[
  {
    "scene_id": "scene_price_001",
    "left_table": "dwd_product_snapshot_d",
    "left_field": "platform_id",
    "right_table": "dim_platform",
    "right_field": "platform_id",
    "join_type": "left",
    "cardinality": "n:1",
    "relation_desc": "商品快照关联平台信息",
    "is_recommended_path": true
  },
  {
    "scene_id": "scene_price_001",
    "left_table": "dwd_product_snapshot_d",
    "left_field": "brand_id",
    "right_table": "dim_brand",
    "right_field": "brand_id",
    "join_type": "left",
    "cardinality": "n:1",
    "relation_desc": "商品快照关联品牌信息",
    "is_recommended_path": true
  },
  {
    "scene_id": "scene_price_001",
    "left_table": "dwd_product_snapshot_d",
    "left_field": "category_id",
    "right_table": "dim_category",
    "right_field": "category_id",
    "join_type": "left",
    "cardinality": "n:1",
    "relation_desc": "商品快照关联品类信息",
    "is_recommended_path": true
  }
]
```

##### `scene_guideline`

```json
[
  {
    "scene_id": "scene_price_001",
    "guideline_type": "metric_definition",
    "guideline_key": "discount_rate",
    "guideline_value": "折扣率默认按 sale_price / list_price 计算；若用户提到折扣力度，则优先比较折扣率变化。"
  },
  {
    "scene_id": "scene_price_001",
    "guideline_type": "default_grain",
    "guideline_key": "product_count",
    "guideline_value": "用户未明确说明时，商品数默认 count(distinct item_id)，而不是 sku_id。"
  },
  {
    "scene_id": "scene_price_001",
    "guideline_type": "default_time_range",
    "guideline_key": "recent_period",
    "guideline_value": "用户提到最近、近期但未给出具体时间时，默认取最近 30 天。"
  },
  {
    "scene_id": "scene_price_001",
    "guideline_type": "bucket_rule",
    "guideline_key": "price_band",
    "guideline_value": "价格带默认分为 0-199、200-399、400-599、600-999、1000+。"
  },
  {
    "scene_id": "scene_price_001",
    "guideline_type": "ambiguity_strategy",
    "guideline_key": "missing_dimension",
    "guideline_value": "如果用户没有说明品牌、平台或品类，则先做总体分布，再给出建议追问。"
  }
]
```

#### 这套案例数据可以直接怎么用

如果你们要快速做演示，最实用的做法是：

1. 用上面的 4 张业务表示例做一份本地测试库。
2. 只先建一个 `商品价格分析` 场景。
3. 把 `scene_table`、`scene_field`、`scene_relation`、`scene_guideline` 录到应用库。
4. 先验证 3 个问题：
   - 最近 7 天 Zara 和优衣库风衣售价变化如何？
   - 天猫和京东的连衣裙折扣力度有什么差异？
   - 最近一周哪些品牌降价最明显？

这样第 1、2 步就不再只是“设计说明”，而是已经有一套可跑的最小数据底座。

---

## 3. 推荐技术方案

如果第一阶段目标是尽快做出可演示、可验证、可继续扩展的 PoC，建议采用下面这套组合。

### 3.1 前端

- `React + Vite + TypeScript`
- 图表库建议使用 `ECharts`
- 数据请求建议使用 `TanStack Query`
- 管理后台和工作台可以共用一个前端项目，不必拆两个仓库

### 3.2 后端

- `FastAPI`
- `SQLAlchemy`
- `Pydantic`
- `Redis` 用于缓存、任务状态或长分析过程的临时状态

### 3.3 数据层

- 应用数据库建议使用 `PostgreSQL`
- 最终业务库沿用现有库型，但必须使用只读账号
- 应用数据库保存配置、会话、日志、报告页，而不是直接改业务库

### 3.4 模型层

- 封装统一的 `LLM Provider Adapter`
- 第一阶段先接一个主模型即可，但接口层要预留多模型切换能力
- Prompt 不要写死在代码里，建议做成模板文件或数据库配置

### 3.5 SQL 校验

- 建议使用 `sqlglot` 做 SQL 解析和语法树检查
- 必须做白名单表校验、禁用字段校验、只读校验、行数限制、超时控制

### 3.6 PPT 导出

- 建议单独使用 `PptxGenJS` 生成 `.pptx`
- 原因是第一阶段要求“可编辑”，而不是导出成图片拼接的假 PPT

---

## 4. 系统总体架构

第一阶段建议按下面 5 层实现。

### 4.1 展示层

- 分析工作台
- 场景选择页
- 管理后台
- 报告预览页

### 4.2 应用服务层

- 场景配置服务
- 对话分析服务
- SQL 执行服务
- 图表结果服务
- 报告服务

### 4.3 模型编排层

- 问题理解
- 分析计划拆解
- SQL 生成
- 结果解释
- 下一步建议生成

### 4.4 数据访问层

- 最终业务库只读访问
- 元数据缓存
- 应用库配置读取
- 会话和日志持久化

### 4.5 导出与观测层

- PPT 页面生成
- 查询日志
- 模型调用日志
- 错误追踪与调试记录

---

## 5. 实施前必须先确认的前置条件

如果下面几个问题没有确认，系统可以写代码，但做出来很容易不可信。

### 5.1 商品主键和跨快照识别方式

需要确认：

- 同一商品在历史快照中靠什么识别
- 是否存在稳定的 `item_id` 或等价标识
- SKU 和 SPU 的分析层级怎么选

### 5.2 核心业务口径

至少先定义清楚：

- 上新商品
- 在售商品
- 价格字段优先级
- 折扣计算规则
- 爆款和异常变化的基础定义

### 5.3 快照稳定性

需要确认：

- 是否按天快照
- 是否有缺天
- 历史属性是否覆盖还是保留版本

### 5.4 首批分析场景

第一阶段不要一开始做太多，建议先定 3 个：

1. 商品价格分析
2. 竞品对比分析
3. 商品结构分析

### 5.5 真实问题样本

至少收集 `20-50` 条业务分析师真实问题，用于：

- Prompt 调试
- SQL 准确性验证
- 验收测试

---

## 6. 分步实施方案

下面按推荐顺序说明第一阶段应该怎么做。

## 6.1 第 0 步：冻结范围和验收口径

### 目标

把第一阶段真正要交付的内容定死，避免开发过程中不断加需求。

### 需要做什么

- 用 PRD 开一次实现评审会。
- 明确第一阶段 `In Scope` 和 `Out of Scope`。
- 确认首批分析场景数量，不超过 3 个。
- 确认导出物就是 `.pptx`，且以“可编辑”为目标。
- 确认不做登录、权限、多人协作、写回数据库。
- 确认首轮成功标准，例如：
  - 用户不用写 SQL 也能完成分析
  - 可以查看 SQL 和数据来源
  - 每轮分析可追加到报告

### 产出物

- 范围冻结清单
- 第一阶段验收清单
- 首批场景清单

### 完成标准

- 产品、技术、数据三方对第一阶段边界没有分歧
- 后续开发不再围绕范围反复讨论

---

## 6.2 第 1 步：做数据盘点和业务口径确认

### 目标

确认业务库能不能支持这个系统，先把数据基础打稳。

### 需要做什么

- 盘点所有候选表。
- 识别主表、维表、快照表、属性表、价格表、上下架状态表。
- 梳理品牌、平台、品类、商品、SKU 的关系。
- 明确关键字段：
  - 主键字段
  - 时间字段
  - 价格字段
  - 平台字段
  - 品牌字段
  - 品类字段
- 梳理表之间的 join 关系。
- 标注高风险字段和不允许暴露的字段。
- 跟业务一起确认口径词典。
- 用真实问题反推数据是否支撑。

### 产出物

- 数据盘点表
- 首版 ER 关系图
- 业务口径词典
- 数据风险清单

### 完成标准

- 至少能支撑 3 个场景的基础查询
- 关键口径没有重大歧义
- 能说明哪些问题现在做不了

---

## 6.3 第 2 步：搭建项目骨架和应用数据库

### 目标

先把工程基础搭起来，让后续模块都有地方落。

### 需要做什么

- 创建前端项目和后端项目。
- 建立开发、测试、本地演示环境。
- 配置应用数据库。
- 配置只读业务库连接。
- 封装模型调用适配层。
- 建立日志、配置、环境变量规范。
- 建立基础目录结构。

### 推荐目录

```txt
fz_workflow/
  backend/
    app/
      api/
      services/
      agents/
      sql/
      models/
      schemas/
      repositories/
  frontend/
    src/
      pages/
      components/
      hooks/
      services/
  prompts/
  docs/
```

### 应用数据库中第一批必须建的表

- `analysis_scene`
- `scene_table`
- `scene_field`
- `scene_relation`
- `scene_guideline`
- `analysis_session`
- `analysis_message`
- `analysis_run`
- `sql_query_log`
- `report_deck`
- `report_page`

### 产出物

- 可启动的前后端工程
- 应用库表结构
- 配置样例和环境说明

### 完成标准

- 前后端都能本地启动
- 后端能连应用库
- 后端能用只读账号连业务库

---

## 6.4 第 3 步：先实现管理员配置后台

### 目标

把“场景化建模”做出来，因为这是系统可控的核心。

### 需要做什么

- 实现场景管理：
  - 创建场景
  - 编辑场景说明
  - 发布和停用场景
- 实现表管理：
  - 选择场景可用表
  - 标记主表
  - 标记推荐用途
- 实现字段管理：
  - 字段中文名
  - 字段说明
  - 字段别名
  - 是否可筛选
  - 是否可分组
  - 是否可聚合
  - 是否可排序
  - 是否时间字段
  - 是否禁用
- 实现 ER 关系配置：
  - 配连接字段
  - 配关系说明
  - 配推荐 join 路径
- 实现分析指导语配置：
  - 默认分析维度
  - 优先表
  - 模糊问题处理策略
  - 常用业务术语解释

### 这一步为什么要优先做

因为没有这层配置，LLM 会直接面对数据库结构，SQL 准确性会不稳定，后面调 Prompt 会很痛苦。

### 产出物

- 管理后台页面
- 配置 CRUD API
- 可发布的场景配置

### 完成标准

- 管理员能从零创建一个完整场景
- 该场景能绑定表、字段、关系、指导语

---

## 6.5 第 4 步：实现 SQL 安全执行链路

### 目标

先把“可执行且安全的只读查询链路”做稳定，再接 LLM。

### 需要做什么

- 实现 SQL 执行服务。
- 强制数据库只读账号连接。
- 做 SQL 语法树校验，至少拦住：
  - 非 `SELECT`
  - 非白名单表
  - 禁用字段
  - 可疑大结果集
  - 绕开推荐关系的高风险 join
- 增加统一限制：
  - 默认 `LIMIT`
  - 查询超时
  - 最大返回行数
- 实现查询结果标准化。
- 保存查询日志和执行摘要。

### 查询结果标准化建议

后续图表、结论、PPT 都不要直接吃原始 SQL 结果，而是统一转成类似结构：

```json
{
  "columns": [],
  "rows": [],
  "metrics": [],
  "chart_candidates": [],
  "sql_text": "",
  "source_tables": [],
  "source_fields": []
}
```

### 产出物

- SQL 校验器
- SQL 执行器
- 结果标准化模块
- 查询日志表和接口

### 完成标准

- 手工输入一条合法 SQL 可以被安全执行
- 非法 SQL 一定会被拦截
- 结果可被前端和报告模块复用

---

## 6.6 第 5 步：实现 LLM 分析 Agent

### 目标

让系统能根据场景配置和用户问题，自动完成分析链路。

### 需要做什么

- 实现问题理解模块：
  - 抽取分析对象
  - 抽取时间范围
  - 抽取维度和指标
  - 判断问题是否模糊
- 实现场景上下文组装模块：
  - 当前场景
  - 可用表字段
  - ER 关系
  - 指导语
  - 历史会话
- 实现 SQL 生成模块。
- 实现多步分析模块：
  - 先做首轮分析
  - 根据结果决定是否追加二次查询
  - 生成下一步建议
- 实现解释模块：
  - 输出业务结论
  - 输出关键数字
  - 输出数据限制提示

### 推荐拆成 4 个小能力，不要一开始做成一个超大 Prompt

1. `Question Parser`
2. `Analysis Planner`
3. `SQL Generator`
4. `Insight Writer`

### 多轮会话至少要保存的信息

- 用户问题
- 系统回答
- 已执行 SQL
- 生成的图表
- 本轮核心结论
- 当前报告页序

### 异常处理必须做

- 问题太模糊时，先反问用户
- SQL 执行失败时，允许有限重试
- 数据为空时，不要瞎编结论
- 结果不充分时，明确提示需要补充条件

### 产出物

- Agent 编排服务
- Prompt 模板
- 多轮会话持久化

### 完成标准

- 用户输入一个典型问题后，系统能返回：
  - 结论
  - 图表数据
  - SQL
  - 使用的表字段
  - 下一步建议

---

## 6.7 第 6 步：实现分析工作台前端

### 目标

把分析链路做成业务用户能直接使用的 Web 界面。

### 需要做什么

- 实现场景选择页。
- 实现分析工作台页。
- 实现多轮对话区。
- 实现图表展示区。
- 实现 SQL 查看入口。
- 实现数据来源查看入口。
- 实现推荐问题和下一步建议。
- 实现加载中、失败、重试等状态反馈。

### 工作台最小布局建议

- 左侧：会话历史或推荐问题
- 中部：对话和结论
- 右侧：图表、SQL、数据来源、报告预览

### 前端这一步要特别注意

- 普通业务用户首先看结论和图表，不要让 SQL 抢主视图。
- SQL 查看入口要明显，但不要强打断主流程。
- 长分析过程必须有状态提示，否则用户会以为系统卡死。

### 产出物

- 场景选择页
- 分析工作台页
- 历史会话查看能力

### 完成标准

- 业务用户可以独立完成一轮问题输入、结果查看和继续追问

---

## 6.8 第 7 步：实现图表和结果呈现规则

### 目标

让查询结果稳定地变成正确、易懂、可复用的图表和结论。

### 需要做什么

- 定义图表配置协议。
- 做图表选型规则：
  - 趋势类优先折线图
  - 对比类优先柱状图
  - 构成类优先堆叠图或环形图
  - TopN 类优先排行榜
- 对同类结果建立统一渲染模板。
- 规范结论文案：
  - 要有关键数字
  - 要有清晰比较对象
  - 必要时提示口径限制

### 建议统一前后端之间的图表结构

```json
{
  "chart_type": "line",
  "title": "",
  "x_field": "",
  "y_field": "",
  "series": [],
  "table_preview": []
}
```

### 产出物

- 图表协议
- 图表渲染器
- 文案生成规范

### 完成标准

- 相同类型结果不会每次用不同的图表表达
- 图表和结论之间不打架

---

## 6.9 第 8 步：实现“对话即报告”的 PPT 模块

### 目标

把每轮有效分析自动沉淀为 PPT 页面，并支持连续追加。

### 需要做什么

- 定义报告页数据结构。
- 设计 2 到 3 个基础模板：
  - 封面页
  - 内容页
  - 总结页
- 规定每页最少包含：
  - 页面标题
  - 核心结论
  - 图表
  - 关键数据点
  - 简短业务解释
- 做“会话绑定 deck”的机制。
- 每轮有效分析后自动写入 `report_page`。
- 实现报告预览页。
- 实现导出 `.pptx`。

### 报告页建议结构

```json
{
  "page_type": "analysis",
  "title": "",
  "summary": "",
  "chart_spec": {},
  "key_metrics": [],
  "notes": "",
  "source_summary": ""
}
```

### 这一步最关键的验证点

- 导出的文件能正常打开
- 文本可编辑
- 页序正确
- 同一会话可持续追加新页

### 产出物

- 报告页数据模型
- PPT 生成服务
- 报告预览和导出接口

### 完成标准

- 任意一次分析会话结束后都能导出完整可编辑 PPT

---

## 6.10 第 9 步：测试、评估和验收

### 目标

把系统从“能跑”推进到“能用、可信、可演示”。

### 需要做什么

- 建立一套真实问题测试集。
- 逐题验证：
  - 是否理解问题
  - 是否选对表和字段
  - SQL 是否正确
  - 图表是否合理
  - 结论是否可信
  - PPT 是否正常
- 做失败场景测试：
  - 模糊问题
  - 空结果
  - 字段缺失
  - 查询超时
  - 快照不完整
- 做多轮会话测试：
  - 继承上下文
  - 改变方向
  - 继续追加报告

### 建议验收指标

- 典型问题首轮成功率
- SQL 可接受率
- 图表与结论一致性
- 首轮结果耗时
- `.pptx` 导出成功率

### 产出物

- 测试题库
- 验收记录
- 问题清单和修复清单

### 完成标准

- 满足 PRD 中场景配置、分析能力、可解释性、报告能力四类验收项

---

## 6.11 第 10 步：本地演示部署和迭代闭环

### 目标

把系统放到可持续演示和持续改进的状态。

### 需要做什么

- 部署本地演示版或测试环境。
- 配置日志和错误跟踪。
- 保留模型调用记录和 SQL 摘要。
- 建立用户反馈收集机制。
- 每周复盘：
  - 哪些问题答得差
  - 哪些 SQL 经常失败
  - 哪些场景缺字段或口径说明

### 产出物

- 可访问的演示环境
- 使用反馈表
- 周期性优化清单

### 完成标准

- 业务同事可以直接拿真实问题试用
- 团队知道下一轮优先改什么

---

## 7. 第一阶段建议的开发顺序

如果资源有限，不要并行铺太开，建议按下面顺序推进：

1. 先做数据盘点和口径确认。
2. 再做应用库和管理员配置后台。
3. 然后做 SQL 安全执行链路。
4. 再接 LLM 的单轮分析。
5. 单轮稳定后，再做多轮会话。
6. 然后补图表呈现。
7. 最后接 PPT 持续生成和导出。

这个顺序的原因很简单：

- 没有数据口径，后面全是伪准确。
- 没有配置后台，LLM 很难稳定。
- 没有 SQL 安全链路，系统不敢上线演示。
- 没有单轮稳定，多轮只会放大错误。

---

## 8. 核心接口建议

第一阶段不需要接口特别复杂，但以下接口基本是必须的。

### 8.1 场景配置相关

- `POST /api/scenes`
- `GET /api/scenes`
- `GET /api/scenes/{scene_id}`
- `PUT /api/scenes/{scene_id}`
- `POST /api/scenes/{scene_id}/publish`

### 8.2 分析相关

- `POST /api/analysis/sessions`
- `GET /api/analysis/sessions/{session_id}`
- `POST /api/analysis/sessions/{session_id}/ask`
- `GET /api/analysis/sessions/{session_id}/messages`

### 8.3 报告相关

- `GET /api/reports/{deck_id}`
- `POST /api/reports/{deck_id}/pages`
- `POST /api/reports/{deck_id}/export`

### 8.4 管理元数据相关

- `POST /api/scenes/{scene_id}/tables`
- `POST /api/scenes/{scene_id}/fields`
- `POST /api/scenes/{scene_id}/relations`
- `POST /api/scenes/{scene_id}/guidelines`

---

## 9. 第一阶段可以明确不做的内容

为了保证可落地，下面这些建议明确排除在第一阶段外：

- 登录、权限、多租户
- 定时报表推送
- 数据回写
- 自动爬虫和 ETL
- 多人协作报告
- 复杂版本管理
- 高度定制的企业模板中心
- 指标平台和语义指标中心

---

## 10. 实施过程中的 5 个高风险点

### 10.1 商品唯一标识不稳定

如果同一商品在不同快照中无法稳定识别，趋势分析和竞品分析都会失真。

### 10.2 业务口径不统一

如果“上新”“在售”“折扣”没有统一定义，系统看起来会答题，实际上是在混用口径。

### 10.3 直接依赖 Prompt 修正数据问题

数据问题不能靠 Prompt 补，Prompt 只能约束表达，不能修复底层数据。

### 10.4 图表和结论脱节

如果只让模型生成结论，不绑定关键数字，结果会变得像咨询报告，不像数据分析系统。

### 10.5 PPT 导出最后才做

如果前面不统一结果结构，最后接 PPT 会返工，因为页面数据没有标准格式。

---

## 11. 推荐的里程碑拆分

如果团队是一个小团队，建议按下面方式切里程碑。

### Milestone 1：打通基础链路

- 完成数据盘点
- 完成应用库表设计
- 完成管理员配置后台

### Milestone 2：打通单轮分析

- 完成 SQL 安全执行链路
- 完成单轮分析 Agent
- 完成基本图表渲染

### Milestone 3：打通多轮和报告

- 完成多轮会话
- 完成报告页累计
- 完成 `.pptx` 导出

### Milestone 4：进入试用和修正

- 用真实问题做回归测试
- 修正场景口径和 Prompt
- 提升 SQL 准确率和结果可信度

---

## 12. 最终建议

这类系统最容易犯的错误，是一开始就把重点放在“大模型很聪明”上。正确顺序应该是：

1. 先把数据和口径盘清楚。
2. 再把场景和语义层建起来。
3. 然后做安全 SQL 执行。
4. 再让 LLM 参与分析。
5. 最后把结果稳定地变成图表和 PPT。

只要这个顺序不乱，第一阶段就能做出一个真正可演示、可验证、可继续扩展的商品数据分析工作台。
