# deerflow_bridge

这里放与 `deer-flow/` 的桥接层，不把业务逻辑直接写进上游仓库。

目标：

1. 调用 DeerFlow runtime
2. 注册业务 skills
3. 读取 / 写入 memory 与 preferences
4. 接入 deep research

当前桥接层策略：

1. 优先通过 `deer-flow/backend/packages/harness/deerflow/client.py` 做嵌入式调用
2. 不直接改上游 DeerFlow 代码
3. 如果本地 DeerFlow 不可用，则自动降级为 stub 模式

当前已暴露的 bridge 能力：

- `health`
- `list_skills`
- `get_memory`
- `invoke(chat | list_skills | get_memory | fallback)`

原则：

- 优先桥接
- 少改上游
- 保留后续升级空间
