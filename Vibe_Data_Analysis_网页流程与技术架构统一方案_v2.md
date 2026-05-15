# Vibe Data Analysis 网页流程与技术架构统一方案 v2

## 1. 文档目的

这份文档用于解决一个实际问题：  
现有资料已经覆盖了业务目标和总体架构，但对“网页到底怎么跑、每一步页面和后端如何联动”描述不够完整。

本方案把三部分统一起来：

1. 业务流程（客户看到的过程）
2. 技术架构（系统如何实现）
3. 网页落地（当前 demo 的页面、接口、数据结构一一对应）

---

## 2. 与既有文档的关系

本方案继承并统一以下文档：

- `vibe-data-analysis-agent-architecture.md`：定义主链路和模块职责
- `服装商品数据LLM分析应用-产品需求文档(PRD).md`：定义第一阶段业务场景和验收方向
- `服装商品数据LLM分析应用_实现方案与分步落地指南.md`：定义分阶段实施逻辑
- `面料主动企划系统_开发文档_V1.md`：提供多智能体和工程化抽象思路

本方案修正点：

1. 不再只讲“目标架构”，必须映射到当前网页可操作流程。
2. 不再把“关系确认”当高门槛人工动作，改为“系统建议 + 用户确认”。
3. 场景不再是单一测试问题，改为预置多场景、多目标样例可直接测试。

---

## 3. 一句话系统定义

这是一个以 `Analysis Session` 为中心的对话式分析工作台：  
用户先选场景并确认语义层（字段语义 + ER 关系），再输入分析目标，系统自动完成 `Query Plan -> SQL -> Result -> Insight -> Report`。

---

## 4. 业务流程（面向客户演示）

## 4.1 主流程

1. 创建或选择业务场景。  
2. 查看场景字段语义，确认分析维度与指标口径。  
3. 查看 ER 关系建议，采用或微调关系。  
4. 输入分析目标（可用场景内置样例）。  
5. 系统生成 Query Plan 与 SQL 并执行。  
6. 返回结果、洞察、下一步建议。  
7. 持续多轮分析并导出报告。

## 4.2 为什么要先做“场景配置”

因为业务分析的核心风险不是“SQL 语法错”，而是“口径和关联关系错”。  
先确认场景语义层，能显著降低以下问题：

- 表关联路径错误导致重复计数
- 指标解释错误导致业务结论偏差
- 多轮分析前后口径不一致

---

## 5. 场景体系（第一阶段全纳入）

当前已按 PRD 预置并落地以下场景：

1. 竞品与价格分析
2. 商品结构分析
3. 趋势与爆款分析
4. 商品价格分析（用于快速总览）

每个场景包含：

- 场景描述
- 字段语义配置（metric/dimension/time/filter）
- 默认 ER 关系
- 多条 sample_goals（非单一测试目标）

---

## 6. 网页信息架构（当前已实现）

## 6.1 页面分区

当前单页工作台包含 6 个区域：

1. 会话区：创建 `session_id`
2. 配置中心-场景：创建场景、刷新、同步预置场景、场景选择
3. 配置中心-字段语义：新增字段业务定义
4. 配置中心-ER关系：手工新增关系 + 自动建议 + 一键采用
5. 分析区：目标输入、样例目标一键填充、执行分析、导出报告
6. 结果区：Query Plan、SQL、结果预览、洞察、推荐下一步

## 6.2 小字体关键说明（已加）
- 数据库连接前需要哪些信息
- 为什么要做 ER 关系确认
- 关系确认的推荐流程（自动建议优先）

---

## 7. 技术架构（当前实现）

## 7.1 运行模式

系统支持两种模式：

1. 在线模式：`FastAPI`（依赖安装成功时）
2. 离线模式：`stdlib HTTP server`（依赖安装失败自动降级）

业务接口在两种模式中保持一致，避免“模式切换导致前端不可用”。

## 7.2 核心模块

1. `config_store.py`  
持久化场景、字段语义、关系配置，管理预置场景。

2. `engine.py`  
负责 Query Plan、SQL 生成、只读校验、执行、洞察、报告导出。

3. `main.py` / `run_demo.py`  
提供统一 API（在线/离线两套后端）。

4. `index.html`  
前端工作台，串联配置流程与分析流程。

---

## 8. 接口清单（网页与后端映射）

## 8.1 配置相关

- `GET /api/config/scenes`：场景列表
- `POST /api/config/scenes`：创建场景
- `POST /api/config/scenes/preset-sync`：同步预置场景
- `GET /api/config/scenes/{scene_id}`：场景详情
- `POST /api/config/scenes/{scene_id}/fields`：新增字段语义
- `POST /api/config/scenes/{scene_id}/relations`：新增关系
- `GET /api/config/relation-suggestions`：关系建议（含置信度/原因）

## 8.2 分析相关

- `POST /api/sessions`：创建会话
- `POST /api/sessions/{session_id}/analyze`：执行分析目标
- `GET /api/sessions/{session_id}`：查看会话

## 8.3 报告相关

- `POST /api/sessions/{session_id}/export`：导出报告
- `GET /api/download/{session_id}`：下载文件

---

## 9. 关系确认机制（解决“用户很难确认”）

## 9.1 当前机制

当前采用“半自动确认”：

1. 系统给出候选关系（字段名和维表键匹配）  
2. 返回每条关系的置信度和理由  
3. 用户可单条采用或一键采用  
4. 用户只需处理少量低置信或业务特例关系

## 9.2 这套机制的价值

- 降低业务用户理解成本
- 提升 SQL 生成稳定性
- 降低错误关联导致的分析偏差
- 把关系知识沉淀为可复用资产

## 9.3 后续增强（下一步）

建议把关系建议升级为三层推断：

1. 主外键推断
2. 字段名语义匹配推断
3. 数据采样一致性验证推断

---

## 10. 数据与配置模型

## 10.1 场景对象

场景至少包含：

- `scene_id`
- `name`
- `description`
- `sample_goals[]`
- `fields[]`
- `relations[]`

## 10.2 字段语义对象

字段语义至少包含：

- `table_name`
- `field_name`
- `semantic_name`
- `description`
- `role`（metric/dimension/time/filter）
- `enabled`

## 10.3 关系对象

关系至少包含：

- `left_table.left_field`
- `right_table.right_field`
- `join_type`
- `note`

---

## 11. 从“目标输入”到“结果输出”的执行链

## 11.1 执行步骤

1. 前端提交分析目标  
2. 后端生成 Query Plan（意图、指标、维度、图表建议）  
3. 后端生成 SQL  
4. SQL 安全校验（只读、危险关键字拦截）  
5. 查询数据库并返回结果  
6. 生成洞察文本和下一步建议  
7. 形成可导出报告页

## 11.2 当前边界

当前是规则化规划 + SQL 模板生成，已适合演示和流程验证。  
后续可替换或结合为 LLM Planner/Generator（agaent），但接口契约不需要重做。

---

## 12. PPT 模块专项设计（补全）

## 12.1 来自旧文档的硬要求

根据 PRD 与架构文档，PPT 模块必须满足：

1. 对话即报告：每轮有效分析自动生成一页  
2. 会话绑定 deck：同一会话持续追加、可回看  
3. 导出可编辑 `.pptx`：文本可编辑、结构可读  
4. 页面包含来源链路：表/SQL/关键指标可追溯  
5. 模板策略分层：先统一基础模板，再做企业模板升级

## 12.2 当前网页与后端落地状态

当前 demo 已实现最小可用版本：

1. 每次 `/analyze` 产生 `slide` 对象  
2. 会话内 `slides[]` 持续累积  
3. `/export` 统一导出报告文件  
4. 在线模式优先 `.pptx`，离线模式兜底 `.md`

## 12.3 推荐的页面数据结构

建议统一 `slide_payload`，避免后续返工：

1. `page_type`：overview/comparison/trend/root_cause/risk/summary  
2. `title`：页面标题  
3. `narrative`：核心结论  
4. `chart_spec`：图表配置  
5. `key_metrics`：关键数字  
6. `lineage`：来源表、SQL 摘要、时间窗口  
7. `next_actions`：下一步建议

## 12.4 页面类型策略

与 `Next Goal Recommender` 对齐，固定 6 类页面：

1. 概览页（Overview）  
2. 对比页（Comparison）  
3. 趋势页（Trend）  
4. 归因页（Root Cause）  
5. 风险与建议页（Risk & Action）  
6. 总结页（Summary）

## 12.5 导出验收标准

每次导出应自动检查：

1. 文件可打开  
2. 文本可编辑（不是图片）  
3. 页序与会话顺序一致  
4. 每页有标题与关键结论  
5. 每页可回溯 lineage

## 12.6 PPT 开源参考与取舍

先说明结论：  
在你之前四份资料里，已提到 `PptxGenJS` 和 `pptx-automizer`，未明确提到 Banana Slides。

### A. PptxGenJS（推荐基础方案）

仓库：`gitbrent/PptxGenJS`  
链接：https://github.com/gitbrent/PptxGenJS

适合：结构化数据稳定生成可编辑 `.pptx`。

### B. pptx-automizer（推荐模板增强）

仓库：`singerla/pptx-automizer`  
链接：https://github.com/singerla/pptx-automizer

适合：已有企业模板，需要替换模板指定元素。

### C. Banana Slides（交互体验参考）

仓库：`Anionex/banana-slides`  
链接：https://github.com/Anionex/banana-slides

价值：可借鉴“口头改稿 + 快速页面迭代”的交互方式。  
限制：更偏创意表达，不等价于可追溯的数据分析报告引擎。

### D. PPTAgent（研究型参考）

仓库：`icip-cas/PPTAgent`  
链接：https://github.com/icip-cas/PPTAgent

价值：可借鉴“规划-编辑-反思”的自动修订流程。

### E. Presenton（端到端开源 AI PPT 参考）

仓库：`presenton/presenton`  
链接：https://github.com/presenton/presenton

价值：  
提供了从 AI 生成到模板化输出的一体化路径，可作为“独立 PPT 子系统”参考。

## 12.7 选型建议（结合当前项目）

当前实现调整：

1. 继续保持结构化导出链路（当前已通）  
2. PPT 模块收敛到 `presenton/presenton`，优先使用其 AI Presentation Generation API  
3. `python-pptx` 仅作为 Presenton 不可用时的临时兜底  
4. 后续模板和页面编辑优先沿 Presenton 的模板/编辑能力扩展

---

## 13. 为什么这套方案能同时解释“旧文档”和“网页”

因为它完成了三层映射：

1. 业务层：PRD 的三大核心场景已落地为可选择场景和样例目标  
2. 架构层：`Session + ER + QueryPlan + SQL + Insight` 主链路已实现  
3. 交互层：网页每个按钮都能映射到明确 API 和数据对象

所以，这不是“新写一份文档”，而是把之前抽象方案变成网页可执行模型。

---

## 14. 已知限制与改进优先级

## 14.1 已知限制

1. 关系建议目前是规则模板，未做真实跨表采样验证
2. SQL 生成策略仍偏模板化
3. 报告输出目前先保证可编辑和链路完整，视觉模板较基础

## 14.2 优先改进顺序

1. 关系建议升级为真实元数据推断 + 样本验证  
2. SQL 生成升级为 Query Plan 驱动的策略路由  
3. 增加场景级口径规则（比如折扣定义、上新定义）  
4. 报告模板升级为客户品牌样式

---

## 15. 演示操作建议（给业务或客户）

1. 点击“同步预置场景”
2. 选择“竞品与价格分析”
3. 点击“加载关系建议”后“一键采用建议”
4. 在样例目标下拉中选择一个目标并填充
5. 执行分析，展示 Query Plan、SQL、结果、洞察
6. 切换到“趋势与爆款分析”再跑一轮
7. 导出报告并展示可追溯链路

这条演示路径可以完整体现：  
“场景配置 -> 关系确认 -> 目标分析 -> 报告输出”的产品闭环。
