# vibe_backend

这是新的正式后端服务。

职责：

- 提供独立 `API`
- 管理 `AnalysisSession`
- 管理 `Scene / Schema / Deck / Artifact`
- 调用 `DeerFlow Runtime Bridge`

当前已跑通第一条后端功能闭环：

- `Scene`
- `AnalysisSession`
- `QueryPlan`
- `QueryRun`
- `SlideDraft`
- `Deck`
- `Artifact`

当前状态：

- 默认不保留运行测试数据（`VIBE_PERSIST_STATE=0`），仅内存态；如需落盘可设置 `VIBE_PERSIST_STATE=1`
- 查询链路默认连接真实 MySQL（通过 `MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DATABASE` 配置）
- 导出产物默认写入 `runtime_data/artifacts/`
- `Deck export` 已生成真实 `.pptx` 文件，并附带 `json/md/html` sidecar
- DeerFlow bridge 已支持 thread 初始化、skills/memory 探测、chat turn 持久化
- 可通过 `Scene -> Session -> Query -> Slide -> Deck -> Export` 走通完整链路
- 已新增独立 `LLM-agent` 模块（不依赖 deepflow），支持 `recommend -> validate -> apply -> publish`（无服务端草稿持久化）

LLM-agent 独立接口：

```bash
GET  /api/v1/llm-agent/health
GET  /api/v1/llm-agent/cache
POST /api/v1/llm-agent/cache/refresh
GET  /api/v1/scenes/cache
POST /api/v1/scenes/cache/refresh
POST /api/v1/llm-agent/scenes/{scene_id}/recommend
POST /api/v1/llm-agent/scenes/{scene_id}/validate
POST /api/v1/llm-agent/scenes/{scene_id}/apply
POST /api/v1/llm-agent/scenes/{scene_id}/publish
```

LLM-agent 环境变量（可选）：

```bash
LLM_AGENT_PROVIDER=heuristic|http|modelscope|codex|codex_cli
LLM_AGENT_HTTP_ENDPOINT=http://...
LLM_AGENT_API_KEY=...
LLM_AGENT_HTTP_MODEL=Qwen/Qwen3-235B-A22B-Instruct-2507
LLM_AGENT_HTTP_TIMEOUT=30
LLM_AGENT_SCHEMA_CACHE_TTL_SECONDS=300
SCENE_CACHE_TTL_SECONDS=300

# 当 provider=codex/codex_cli 时生效
LLM_AGENT_CODEX_BIN=codex
LLM_AGENT_CODEX_MODEL=gpt-5
LLM_AGENT_CODEX_HOME=/path/to/provider-home
LLM_AGENT_CODEX_HOME_FALLBACKS=/path/to/fallback-home-1,/path/to/fallback-home-2
LLM_AGENT_CODEX_CWD=/path/to/repo
LLM_AGENT_CODEX_BYPASS_SANDBOX=0

# SQL 结果 agent
SQL_RESULT_AGENT_PROVIDER=codex|codex_cli|http|modelscope
SQL_RESULT_AGENT_HTTP_ENDPOINT=https://...
SQL_RESULT_AGENT_API_KEY=...
SQL_RESULT_AGENT_HTTP_MODEL=Qwen/Qwen3-235B-A22B-Instruct-2507
SQL_RESULT_AGENT_CODEX_HOME=/path/to/provider-home
SQL_RESULT_AGENT_CODEX_HOME_FALLBACKS=/path/to/fallback-home-1,/path/to/fallback-home-2
```

示例（gmn 失败自动切到 aixj_vip）：

```bash
LLM_AGENT_PROVIDER=codex_cli
LLM_AGENT_CODEX_HOME=/path/codex_gmn
LLM_AGENT_CODEX_HOME_FALLBACKS=/path/codex_aixj_vip

SQL_RESULT_AGENT_PROVIDER=codex_cli
SQL_RESULT_AGENT_CODEX_HOME=/path/codex_gmn
SQL_RESULT_AGENT_CODEX_HOME_FALLBACKS=/path/codex_aixj_vip
```

本地启动：

```bash
bash scripts/start_vibe_backend.sh
```

本地冒烟：

```bash
bash scripts/smoke_test_vibe_backend.sh
```
