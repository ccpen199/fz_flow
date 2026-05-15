# vibe_frontend

这是新的正式前端工作台。

职责：

- 分析会话工作台
- Scene 配置
- ER 关系确认
- Slide / Deck 审核与编辑

当前已补一个正式工作台 MVP：

- 场景创建
- 会话列表切换
- 会话创建
- Query Plan 生成
- Query 执行
- Query 结果表格查看
- Slide Draft 查看
- Deck 批准与导出
- Artifact 下载
- DeerFlow thread 上下文查看
- 内置 `DeerFlow 中文上手清单`

启动前端：

```bash
bash scripts/start_vibe_frontend.sh
```

Mac 一键启动整套：

```bash
bash scripts/start_vibe_stack.sh
```

Windows 一键启动整套：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start_vibe_stack.ps1
```

停止整套：

```bash
bash scripts/stop_vibe_stack.sh
```

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\stop_vibe_stack.ps1
```

默认端口：

- Backend: `18900`
- Frontend: `18901`

如需避免冲突，可在启动前覆写环境变量：

```bash
BACKEND_PORT=18910 FRONTEND_PORT=18911 bash scripts/start_vibe_stack.sh
```
