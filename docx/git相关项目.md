# 纺织面料主动企划系统
## 开发规划与 Git 项目复现映射（V1）

本文档目标：把“1+5 多智能体纺织面料主动企划方案”落到可执行开发计划，并为每个模块给出可复现/可借鉴的 Git 项目。

---

## 1. 需求依据（来自你的原始方案）

| 编号 | 需求依据 | 对应开发目标 |
|---|---|---|
| R1 | 系统定位为“虚拟面料企划总监 + 专家智囊团” | 建立 `Orchestrator + Expert Agents` 架构 |
| R2 | 采用“1+5 可插拔专家矩阵” | Agent 插槽化，支持并行调用、可扩展 |
| R3 | 内外数据双轮驱动（ERP/客诉 + 竞品/评价） | SQL + RAG 双引擎数据底座 |
| R4 | 材质纱线与色彩染整细分决策 | 引入工艺/规则引擎与专家知识库 |
| R5 | 5 分钟内输出企划报告 | 异步任务 + 并行执行 + 缓存优化 |
| R6 | 输出可商用的完整方案（含议价话术） | 标准化报告模板与证据引用机制 |
| R7 | 可分阶段落地（Phase 1/2/3） | 里程碑化实施与可验收 KPI |

---

## 2. 模块实现与可复现项目映射

| 模块 | 对应需求 | 主项目（优先） | 借鉴项目（备选） | 复现要点 |
|---|---|---|---|---|
| M1. 编排层（Orchestrator） | R1, R2, R5 | `langchain-ai/langgraph` | `langgenius/dify`、`crewAIInc/crewAI` | 用状态图定义任务拆解、并行分发、冲突消解、汇总生成 |
| M2. 专家 Agent 框架 | R1, R2 | `shibing624/agentica` | `microsoft/autogen` | 按“需求解析/历史侦探/情报/材质/染整”拆角色，统一输入输出契约 |
| M3. 内部历史数据接入（ERP/客诉） | R3 | `frappe/erpnext` | `buildswithpaul/Frappe_Assistant_Core` | 先只读 API，拉取历史订单、客诉、价格均值，做风险排雷 |
| M4. 纺织垂直业务模型 | R4 | `ParaLogicTech/textile` | `Dokuly-PLM/dokuly` | 借鉴纺织生产与 PLM 流程字段，沉淀纱线/工艺/打样数据结构 |
| M5. 外部情报采集（竞品/VOC/价格） | R3, R5 | `unclecode/crawl4ai` | `scrapy/scrapy`、`ScrapeGraphAI/Scrapegraph-ai` | 采集商品详情、评价、价格变动；清洗后入 RAG |
| M6. RAG 知识引擎 | R3, R4 | `qdrant/qdrant` | `milvus-io/milvus` | 建外部库+内部库双索引，采用混合检索（关键词+向量） |
| M7. 评估与回归 | R6, R7 | `vectara/open-rag-eval` | 自建离线评测集 | 把“方案质量”量化：证据命中率、建议可执行率、预算误差 |
| M8. MCP 工具化接入 | R3, R7 | `buildswithpaul/Frappe_Assistant_Core` | `sansan0/TrendRadar`（MCP 实战） | 为内部数据与外部分析工具提供统一工具协议入口 |
| M9. 工作流模板快速验证 | R2, R7 | `langgenius/dify` | `Hammer1/cozeworkflows` | Phase 1 可用低代码快速验证 prompt+RAG+流程 |

---

## 3. 分阶段开发规划（带项目复现落点）

### Phase 1（2-3 周，MVP）
目标：跑通“统筹 + 需求解析 + 品牌情报 + 报告输出”。

| 工作包 | 复现/借鉴项目 | 产出 |
|---|---|---|
| P1-W1 编排骨架 | `langgraph` + 本机 `gemini-fullstack-langgraph` | 可执行 Orchestrator 工作流 |
| P1-W1 需求解析 Agent | `agentica` | 结构化参数提取 JSON |
| P1-W2 外部采集管道 | `crawl4ai` / `scrapy` | 竞品详情与评价采集入库 |
| P1-W2 RAG 最小闭环 | `qdrant`（或先 SQLite + embedding） | 可检索证据并回填报告 |
| P1-W3 报告输出 | 基于自定义模板 | 生成 Markdown/JSON 报告 |

Phase 1 验收：单条需求 5 分钟内输出报告，且每条关键结论可追溯证据。

### Phase 2（1-2 个月）
目标：补齐“内部历史侦探 + 材质/染整 + 风险规则 + 成本估算”。

| 工作包 | 复现/借鉴项目 | 产出 |
|---|---|---|
| P2-W1 ERP 接入 | `erpnext` + `Frappe_Assistant_Core` | 客诉/订单/均价只读查询 |
| P2-W2 纺织模型沉淀 | `ParaLogicTech/textile` | 纱线/工艺/打样实体模型 |
| P2-W3 规则引擎落地 | 基于 LangGraph 节点实现 | 一票否决 + 软告警 |
| P2-W4 成本测算器 | ERP 价格表 + 工艺参数 | 成本区间与超预算提示 |

Phase 2 验收：可自动识别历史踩坑组合并生成规避方案，预算误差可控。

### Phase 3（3 个月+）
目标：全链路闭环（算料、核价、打样派单、反馈学习）。

| 工作包 | 复现/借鉴项目 | 产出 |
|---|---|---|
| P3-W1 PLM 拓展 | `dokuly`（流程借鉴） | 打样单与状态追踪 |
| P3-W2 MCP 工具生态 | `Frappe_Assistant_Core`、`TrendRadar MCP` | 工具调用标准化 |
| P3-W3 评测平台 | `open-rag-eval` | 方案质量持续评估仪表盘 |

---

## 4. 本机可直接借鉴仓库（已存在）

| 项目 | 用途 | 本机路径 | 远程仓库 |
|---|---|---|---|
| Gemini LangGraph Quickstart | Orchestrator 编排参考 | `/Users/chen/Desktop/Cursor_project/LLM-Langraph/gemini-fullstack-langgraph` | `https://github.com/google-gemini/gemini-fullstack-langgraph-quickstart` |
| Agentica | Multi-Agent + RAG + MCP | `/Users/chen/Desktop/Cursor_project/LLM-Agent/agentica` | `https://github.com/shibing624/agentica` |
| TrendRadar | MCP 服务化与数据分析流程 | `/Users/chen/Desktop/Cursor_project/ai_money/Messages/TrendRadar` | `https://github.com/sansan0/TrendRadar` |
| Coze Workflows | 工作流模板库（快速验证） | `/Users/chen/Desktop/Cursor_project/futuapi/cozeworkflows` | `https://github.com/Hammer1/cozeworkflows` |
| Crawl4AI | 外部网页采集与清洗 | `/Users/chen/Desktop/Cursor_project/crawl/crawl4ai` | `https://github.com/unclecode/crawl4ai` |
| Scrapegraph-ai | LLM 驱动抓取流程参考 | `/Users/chen/Desktop/Cursor_project/crawl/Scrapegraph-ai` | `https://github.com/ScrapeGraphAI/Scrapegraph-ai` |
| github-search | Git 项目挖掘与检索辅助 | `/Users/chen/Desktop/Cursor_project/parttime-job-agnet/github-crawl-project/github-search` | `https://github.com/gwen001/github-search` |

---

## 5. 需补充拉取的关键仓库（当前目录未发现）

| 项目 | 用途 |
|---|---|
| `langgenius/dify` | 低代码快速出 MVP（适合业务先体验） |
| `frappe/erpnext` | 内部业务数据底座（订单/客诉/采购） |
| `buildswithpaul/Frappe_Assistant_Core` | ERPNext 的 MCP 接入层 |
| `ParaLogicTech/textile` | 纺织垂直流程与数据模型参考 |
| `qdrant/qdrant` 或 `milvus-io/milvus` | 向量检索底座 |
| `vectara/open-rag-eval` | RAG/方案质量评估 |

---

## 6. 每一块的“最小复现动作”

### 6.1 编排与专家并行（M1 + M2）
1. 用本机 `gemini-fullstack-langgraph` 复现状态图执行链路。
2. 在节点层替换为你的 5 个专家 Agent。
3. 用 `agentica` 补齐多角色 prompt 和工具调用样例。

### 6.2 外部情报（M5）
1. 用 `crawl4ai` 抓取 3 个竞品详情页与评论页。
2. 标准化字段：标题、成分、克重、价格、评论、时间。
3. 写入向量库并做“闷热/起球/环保”检索验证。

### 6.3 内部历史侦探（M3 + M4）
1. 以 ERPNext 的订单/客诉表结构为蓝本定义你的查询接口。
2. 加入“客诉复发风险”规则，返回 hard/soft 风险等级。
3. 对接纺织工艺实体（纱线、针距、后整理）进行可生产校验。

### 6.4 报告生成与评估（M7）
1. 固定报告 schema（结构化 JSON + Markdown）。
2. 引入 `open-rag-eval` 思路建立 20 条样本评测集。
3. 跟踪指标：时延、证据命中率、预算偏差、采纳率。

---

## 7. 推荐实施顺序（避免过早复杂化）

1. 先做 `LangGraph + Crawl4AI + Qdrant` 的 Phase 1 技术闭环。
2. 再接 `ERPNext` 只读接口，补齐历史侦探与排雷。
3. 最后把 MCP、PLM、自动派单接入做成 Phase 3 自动化。

该顺序可以确保你在 2-3 周内就有可演示 MVP，且不会被企业内系统对接拖慢。
