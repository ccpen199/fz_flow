# PPT 模块资料总结与方案对比

更新时间：2026-05-09

## 资料结论

当前项目资料对 PPT 模块的共识是：

- PPT 不是单次导出按钮，而是“对话即报告”的循环工作流。
- 每轮有效 SQL 分析后，应生成一页 `Slide Draft`。
- Slide 需要支持人工审核、重生成、局部编辑、批准入 Deck。
- 同一会话维护一份 Deck，后续分析持续追加页面。
- 导出物必须是可编辑 `.pptx`，第一阶段优先保证文本可编辑、页序正确、来源可追溯。

主要依据：

- `服装商品数据LLM分析应用-产品需求文档(PRD).md`
  - 每轮有效分析生成一页 PPT。
  - 新一轮分析追加新页，不覆盖旧页。
  - 每页至少包含标题、核心结论、图表、关键数据点、业务解释。
  - 导出的 `.pptx` 需正常打开、可编辑、文本不是图片。
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

`SQL 执行 -> QueryRunDTO -> SlideDraftDTO -> Approve -> DeckDTO -> PPTX Artifact`

本次调整：

- PPT 方案收敛为 `presenton_ai`，不再保留多方案切换。
- 前端“汇报产物”页只展示 Presenton AI PPT 生成方案。
- Slide 中保留方案名、关键数据点、结果画像、query 来源。
- Deck 导出时按 slide_id 找回已批准页面，避免切换方案后旧页丢失。
- 导出器优先调用本地或配置的 `presenton/presenton` 服务，通过大模型生成并导出 `.pptx`。
- Presenton 不可用时，系统可临时回退到 `python-pptx` 生成基础 PPTX，回退信息会写入 artifact sidecar。
- 已拆出独立 `Insight/Narrative Service`，Slide 文案、指标卡和图表协议统一由该服务产出。
- 前端“汇报产物”页已加入结构化 Slide 编辑器，可编辑标题、结论、业务解释、下一步建议和图表类型。

## 当前方案

### Presenton AI PPT 生成 `presenton_ai`

定位：

- 端到端开源 AI PPT 子系统。
- 支持从内容生成 PPT，并导出 `.pptx`。
- 可接 OpenAI、Gemini、Azure OpenAI、Anthropic、Ollama 或 OpenAI-compatible 自定义模型。

适合：

- 需要大模型生成 PPT 叙事和版式。
- 需要后续接入 Presenton 模板、页面编辑和导出能力。
- 需要把分析产物从“结构化 slide draft”升级成“AI 生成成稿”。

页面/导出重点：

- 将 SQL 结果、核心结论、业务解释、行动建议整理为 Presenton 生成输入。
- 调用 `/api/v1/ppt/presentation/generate` 导出 PPTX。
- 保留 session/query/deck 来源链路到 sidecar 文件。

## 后续建议

下一步应继续完善 Presenton 服务化集成：

- 固化 Presenton 本地服务启动脚本和模型配置。
- 将页面编辑入口接到 Presenton `edit_path`。
- 增加模板管理，支持公司模板、主题色、封面页、目录页和附录来源页。
- 对导出的 PPTX 做版式、可编辑对象和下载链路验收。
