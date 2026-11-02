# PPT 模块资料总结与方案对比

更新时间：2026-05-09

## 资料结论

当前项目资料对 PPT 模块的共识是：

- PPT 不是单次导出按钮，而是“对话即报告”的循环工作流。
- 每轮有效 SQL 分析后，应生成一页 `Slide Draft`。
- Slide 需要支持人工审核、重生成、局部编辑、批准入 Deck。
- 同一会话维护一份 Deck，后续分析持续追加页面。
- 页面必须可在 TrainPPTAgent 中编辑，第一阶段优先保证页序正确、版本可追溯和编辑结果可回写。

主要依据：

- `服装商品数据LLM分析应用-产品需求文档(PRD).md`
  - 每轮有效分析生成一页 PPT。
  - 新一轮分析追加新页，不覆盖旧页。
  - 每页至少包含标题、核心结论、图表、关键数据点、业务解释。
- TrainPPTAgent 画布中的页面对象需可编辑，回写后版本和来源需可追溯。
- `服装商品数据LLM分析应用_实现方案与分步落地指南.md`
  - 定义报告页数据结构。
  - 设计 2 到 3 个基础模板。
  - 做会话绑定 Deck、报告预览和导出接口。
- `vibe-data-analysis-agent-architecture.md`
  - SQL 后应经过 `Insight & Narrative Service`，再进入 `PPT Composition Service`。
  - 每页保留表、SQL、图表、结论、建议的来源链路。
- `要求new-file.txt`
  - 明确 PPT 生成是“循环自主 + HITL”的过程。
  - 要生成可编辑 PPT，类似 Claude Code 用 skill 生成 PPT 的体验。

## 当前实现状态

已有链路：

`SQL 执行 -> QueryRunDTO -> SlideDraftDTO -> Approve -> DeckDTO -> TrainPPTAgent handoff -> DeckRevision callback`

本次调整：

- PPT 编辑方案统一为 TrainPPTAgent，不再保留方案切换、导出和下载入口。
- 前端“汇报产物”页只展示页面草稿、Deck 和 TrainPPTAgent 编辑入口。
- Slide 中保留关键数据点、结果画像、query 来源和 lineage。
- Deck handoff 按 slide_id 传递已批准页面；callback 以 `base_revision_id` 做乐观锁并生成新版本。
- 已拆出独立 `Insight/Narrative Service`，Slide 文案、指标卡和图表协议统一由该服务产出。
- 前端“汇报产物”页已加入结构化 Slide 编辑器，可编辑标题、结论、业务解释、下一步建议和图表类型。

## 当前方案

## 后续建议

下一步应继续完善 TrainPPTAgent 集成：

- 固化 handoff JSON、callback JSON 和版本冲突处理。
- 增加模板管理，支持公司模板、主题色、封面页、目录页和附录来源页。
- 对页面编辑、回写、恢复和连续追加做端到端验收。
