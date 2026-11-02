# PPT 多轮协作模块设计

更新时间：2026-09-06

## 1. 目标

将当前“单次 SQL 结果 -> Slide Draft -> Deck -> TrainPPTAgent 编辑”的链路升级为同会话内可持续分析、可编辑、可审核、可回滚的 PPT 工作流。

验收闭环：

1. 用户在一个分析会话中提出数据问题，系统生成 SQL 结果和候选页面。
2. 用户继续提出分析问题时，保留原 QueryRun 和原页面，并追加新页面。
3. 用户说“第 3 页标题改短、换成柱状图”时，不执行 SQL，只生成一个页面修订版本。
4. 用户说“第 3 页改为按品类分析”时，生成新的 QueryRun 和新页面，原页面仍可回看。
5. 用户可在任意 DeckRevision 预览、批准或回滚。
6. TrainPPTAgent 画布中的编辑结果可以回写为新的 DeckRevision，并继续迭代。

## 2. 职责边界

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| `fz_workflow` | 场景约束、SQL、QueryRun、Deck/Slide/Turn 版本、审计和回滚 | 将业务事实交给模型记忆保存 |
| DeepFlow | 会话编排、修订意图规划、工作记忆和恢复 checkpoint | 作为唯一版本库或直接改写 Deck |
| TrainPPTAgent | 编辑器画布、模板/版式能力和可编辑演示文稿编辑 | 保存分析口径、SQL 或最终审批状态 |

MySQL 中的 `PresentationTurn`、`DeckRevision`、`SlideRevision`、`QueryRun` 是唯一事实来源。DeepFlow 的 memory/checkpoint 仅保存工作上下文和用户偏好摘要。

## 3. 数据模型

- `PresentationTurn`：一条用户请求及其路由结果，类型为 `analysis`、`presentation_revision` 或 `rollback`。
- `SlideRevision`：某一页的不可变快照，包含来源 slide、父版本、变更摘要和关联 Turn。
- `DeckRevision`：一个 Deck 的不可变页序和样式快照，包含父版本和关联 Turn。
- `Deck.current_revision_id`：当前在编辑和导出的版本。

所有编辑均创建新 `SlideDraftDTO.slide_id` 和新版本；历史页面不原地覆盖。重启恢复从 `runtime_persistence_service` 保存的工作流状态读取。

## 4. 多轮路由

```text
用户消息
  -> PresentationTurn
  -> 路由
     -> analysis: SQL -> QueryRun -> Slide -> DeckRevision
     -> presentation_revision: DeepFlow/rule planner -> whitelist patch -> SlideRevision + DeckRevision
     -> rollback: 指定 DeckRevision -> 恢复页序与样式
  -> TrainPPTAgent handoff / callback
```

页面修订只允许修改白名单字段：标题、副标题、图表类型、结论、叙述、建议和 Deck 样式 token。修订规划器不能修改 SQL、QueryRun、字段关系或事实数据。

## 5. DeepFlow 接入方式

每个 `AnalysisSession` 使用一个稳定的 `deerflow_thread_id`。发送给 DeepFlow 的上下文只包含：当前用户消息、目标 Deck/Slide、最近少量 Turn、当前页面可编辑字段和页面 lineage 摘要。

DeepFlow 返回受限 JSON patch。`fz_workflow` 对目标页、字段和操作做校验；无效 JSON、越权字段或 DeepFlow 不可用时降级到规则规划器。当前实现保留这个接点，不让 DeepFlow 的自由文本直接写数据库。

## 6. TrainPPTAgent 接入方式

当前已完成双向接入：

1. `fz_workflow` 输出 TrainPPTAgent/PPTist 原生 JSON，包含 Deck、Slide、lineage 和可编辑元素。
2. 页面通过 `TRAIN_PPT_AGENT_EDITOR_URL` 跳转到本地 TrainPPTAgent 编辑器，并附加 `fz_handoff_url` 查询参数。
3. TrainPPTAgent 编辑器启动时自动读取交接包并载入画布。
4. 用户在画布修改后点击“同步到分析会话”，TrainPPTAgent 回传完整编辑文档与 `base_revision_id`。
5. `fz_workflow` 校验基线版本，提取标题、副标题、叙述和图表类型，生成新的 SlideRevision/DeckRevision；基线过期时返回 `409`，避免覆盖其他轮次修改。
6. 编辑器收到新版本后替换页面 ID 和版本基线，可继续编辑并将结果回写为新的 DeckRevision。

相关接口：`GET /api/v1/decks/{deck_id}/train-ppt-agent-handoff`、`POST /api/v1/decks/{deck_id}/train-ppt-agent/callback`。

## 7. 验收用例

1. 连续两次分析分别生成两页，前一页仍存在。
2. “第 1 页标题改为品牌价格结构”仅生成新页面版本，不新增 SQL。
3. 回滚后页序、标题、图表类型和样式 token 恢复到指定版本。
4. DeepFlow 不可用时，常见标题/图表/主题修改仍可执行，且响应标记为规则降级。
5. TrainPPTAgent handoff 包中每页包含 `slide_id`、`query_id`、来源 SQL 摘要和可编辑内容。
6. 回写后的 DeckRevision 包含最新页面内容、页序和样式 token，且可再次 handoff。

## 8. 第一阶段接口

- `POST /api/v1/analysis/sessions/{session_id}/turns`：接收 `analysis`、`presentation_revision` 或 `rollback`；`kind=auto` 时按消息推断。
- `GET /api/v1/analysis/sessions/{session_id}/turns`：读取会话内的轮次和处理状态。
- `GET /api/v1/decks/{deck_id}/revisions`：读取不可变 Deck 版本列表。
- `GET /api/v1/decks/{deck_id}/revisions/{revision_id}`：读取指定版本及其页面快照。
- `POST /api/v1/decks/{deck_id}/revisions/{revision_id}/restore`：基于历史快照生成一个新的当前版本，不覆盖历史记录。
- `GET /api/v1/decks/{deck_id}/train-ppt-agent-handoff`：返回供 TrainPPTAgent 编辑器导入的 JSON 交接包。

当前实现已经落地版本持久化、规则降级、编辑器自动载入、画布回写和乐观锁。外部编辑器状态不会替代 fz_workflow 的 SQL、QueryRun、审批和版本事实来源。
