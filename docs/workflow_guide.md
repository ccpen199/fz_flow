# PPT 模块现状评估与 DeepFlow 接入结论（代码 + PRD 对照）

## 1. 评估范围
本次核对范围包括：
- `slide_service`
- `analysis_sessions`
- `deerflow_bridge`
- `decks/artifact_service/query_service/bridge router`
- DeepFlow `ppt-generation` 现成能力
- PRD 中与 PPT 持续生成、可恢复、可编辑导出相关条款

## 2. 总结论
结论是：**会影响 PPT 产品目标达成，但不影响当前基础导出功能可用。**

具体判断：
- 当前系统可导出 `.pptx`，基础导出链路是可用的。
- 但 PPT 内容生成仍偏骨架化，未达到 PRD 目标中的“页面级可解释、连续追加、恢复后持续追加”的完整体验。
- DeepFlow 侧已有可复用的 PPT 生成能力（含 skill 与脚本），但目前尚未进入主分析链路，仍处于旁路能力状态。

## 3. 关键证据（代码）

### 3.1 当前 PPT 可导出，但内容层仍是骨架
- `slide_service` 显示为骨架版本：
  - [slide_service.py](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/apps/vibe_backend/app/services/slide_service.py#L57)
  - 现状：`当前为骨架版本 slide draft`，`lineage_summary` 字段有限，尚不足以支撑页面级可解释链路。
- 导出能力存在：
  - [artifact_service.py](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/apps/vibe_backend/app/services/artifact_service.py#L235)
  - 现状：`.pptx` 包装/导出可用。

### 3.2 会话恢复缺口会影响“持续追加新页”
- 状态持久化默认关闭：
  - [store.py](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/apps/vibe_backend/app/store.py#L37)
  - 现状：默认 `VIBE_PERSIST_STATE=0`，重启后难以稳定续写 deck，与 PRD 的“恢复后继续追加”目标冲突。

### 3.3 DeepFlow 未接入主分析链
- 主链仍走：
  - [analysis_sessions.py](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/apps/vibe_backend/app/routers/analysis_sessions.py#L218)
  - [sql_result_agent_service.py](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/apps/vibe_backend/app/services/sql_result_agent_service.py#L22)
- Bridge 仍为旁路：
  - [bridge.py](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/apps/vibe_backend/app/routers/bridge.py#L8)
  - [integrations/deerflow_bridge/bridge.py](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/integrations/deerflow_bridge/bridge.py#L184)
- 影响：PPT 主链拿不到 DeepFlow 的任务编排/记忆/恢复能力红利。

### 3.4 DeepFlow 已有现成 PPT 能力
- 已有官方 skill：
  - [ppt-generation/SKILL.md](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/deer-flow/skills/public/ppt-generation/SKILL.md#L1)
  - 能力描述：先规划、逐页生成、最终合成 `pptx`。
- 已有合成脚本：
  - [generate.py](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/deer-flow/skills/public/ppt-generation/scripts/generate.py#L10)
  - 技术路径：基于 `python-pptx` 将素材组装成演示文稿。

## 4. PRD 对照（原始资料）
PRD 对 PPT 的关键要求明确包括：
- 每轮一页
- 同会话持续追加
- 可编辑导出
- 恢复后继续追加

参考：
- [PRD](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/服装商品数据LLM分析应用-产品需求文档(PRD).md#L565)
- [PRD](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/服装商品数据LLM分析应用-产品需求文档(PRD).md#L591)
- [PRD](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/服装商品数据LLM分析应用-产品需求文档(PRD).md#L600)
- [PRD](/Users/chen/Desktop/Cursor_project/ai_money/fz_workflow/服装商品数据LLM分析应用-产品需求文档(PRD).md#L717)

## 5. 影响判定

### 5.1 不受阻部分
- 基础 `.pptx` 导出流程可用。

### 5.2 受影响部分（对产品目标）
- 页面内容深度与可解释性不足，难满足“分析依据可追溯”的目标。
- 会话恢复后持续增量生成不稳，影响“长会话持续产出 deck”。
- DeepFlow 已有能力未纳入主链，导致重复造轮子与体验断裂。

## 6. 接入可行性结论
结论：**可以直接接入，但需要按阶段接入，不建议一次性替换全部主链。**

原因：
- DeepFlow PPT skill 与生成脚本已存在，可复用度高。
- 当前主链尚未依赖 DeepFlow 的任务/记忆模型，直接全量切换风险较高。
- 建议先做“PPT 生成链路接入”，再推进“主分析链融合”。

## 7. PPT 模块整改泳道（M1/M2/M3）

### M1（快速可用，1-2 周）
目标：不改主链，先提升 PPT 可用性。
- 将 DeepFlow `ppt-generation` 作为可选生成后端接入 `slide_service`。
- 保留现有导出接口，新增开关（例如按会话或租户切换）。
- 补齐页面元数据（页标题、结论、图表来源、查询摘要）。

交付标准：
- 同一输入下可稳定输出结构化多页 PPT。
- 导出链路保持兼容，无前端破坏性变更。

### M2（连续生成，2-3 周）
目标：满足“同会话持续追加 + 重启后续写”。
- 打开并治理状态持久化（生产配置改为持久化开启）。
- 定义 deck append 协议（新增页而非覆盖）。
- 增加恢复场景测试：服务重启后继续追加第 N+1 页。

交付标准：
- 会话恢复成功率达标（需定义 SLA）。
- 追加页顺序、页码、上下文引用一致。

### M3（深度融合，3-4 周）
目标：PPT 与分析主链统一到可解释、可追溯架构。
- 将 DeepFlow 的任务编排/记忆能力引入主分析链关键节点。
- 打通 lineage 到页级证据（query/result/chart/insight）。
- 完成 bridge 从旁路到主路的治理（路由、鉴权、观测）。

交付标准：
- 页面级可解释链路可审计。
- 与 PRD 对齐完成验收。

## 8. Jira 拆解建议（模块 / 负责人 / 工时 / 依赖）

| 模块 | 任务 | 建议负责人 | 预估工时 | 依赖 |
|---|---|---|---:|---|
| PPT 服务 | `slide_service` 接 DeepFlow skill（开关化） | 后端 | 3d | DeepFlow skill 调用封装 |
| 导出稳定性 | `artifact_service` 兼容新元数据并回归测试 | 后端 | 2d | M1 接入完成 |
| 会话持久化 | `VIBE_PERSIST_STATE` 生产化配置与回归 | 后端/运维 | 2d | 环境配置权限 |
| 追加机制 | deck append 协议与幂等控制 | 后端 | 3d | 会话持久化 |
| 主链融合 | `analysis_sessions` 到 DeepFlow 编排接点 | 后端架构 | 5d | bridge 改造方案 |
| Bridge 治理 | `deerflow_bridge` 主路化改造+鉴权+监控 | 平台后端 | 4d | 主链融合 |
| 可解释性 | 页级 lineage 字段设计与落库 | 后端/数据 | 4d | 主链融合 |
| 端到端验收 | 按 PRD 场景验收（新增/恢复/导出） | QA | 3d | 上述任务完成 |

> 合计粗估：26 人天（可并行压缩至约 3-5 周）。

## 9. 风险与前置决策
- 是否将 DeepFlow 作为 PPT 唯一后端，还是阶段性双轨（建议双轨）。
- 状态持久化的存储选型与成本（本地/Redis/DB）。
- 页级可解释字段的最小闭环定义需先冻结，否则反复改 schema。

## 10. 最终建议
- 先落地 M1，快速把“骨架 PPT”升级到“可用 PPT”。
- 同步启动 M2，优先解决恢复续写问题（这是 PRD 核心体验差距）。
- 在 M3 再做主链融合，避免一次性大改带来的交付风险。

