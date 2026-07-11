# Runtime Clock Context Design

## Goal

让 NaumiAgent 在所有共用 `AgentEngine` 的交互界面中可靠获知当前本地时间，回答日期、时间和时区问题时不需要调用 shell 或公网时间 API。

## Root Cause

`HarnessContextAssembler` 会在每次模型调用前生成临时运行状态快照，但当前快照只包含工具、Skill、任务、后台任务、调度、Worktree、Pursuit、MCP 和资源状态。静态 system prompt 中的 runtime defaults 也没有时间信息。

因此模型无法从可信上下文获取当前时间，只能尝试受权限控制的命令工具或不稳定的公网服务。该行为增加延迟，也会在 `moderate` 权限模式或网络异常时产生误导性的失败答复。

## Design

### Ownership

当前时间属于每轮变化的运行事实，由 `HarnessContextAssembler` 负责注入，不写入会持久化的基础 system prompt，也不创建独立时间工具。

### Clock Source

组装器接受一个可替换的同步时钟函数，默认调用 `datetime.now().astimezone()`。该值必须是带时区的本地时间。

可替换时钟仅用于确定性测试和未来运行时适配，不作为公共工具或配置项暴露。

### Snapshot Format

Harness 快照新增 `### 当前环境` 段，至少包含：

- 当前本地时间：ISO 8601 秒级时间戳，包含 UTC 偏移。
- 时区：优先使用系统时区名称，并同时保留 UTC 偏移，避免 `CST` 等缩写歧义。
- 使用约束：声明该时间是本轮可信运行事实；回答当前日期或时间时直接使用，不调用 shell 或公网时间服务。

示例：

```text
### 当前环境
- 当前本地时间：2026-07-12T03:22:36+08:00
- 时区：CST (UTC+08:00)
- 时间问题：以上是本轮可信时间；可直接回答，无需调用工具或公网 API。
```

### Refresh Semantics

`_react_loop` 和 `_react_loop_streaming` 已在每次模型调用前重新组装 Harness 快照，因此时钟会在每个 ReAct turn 自动刷新。快照仍保持临时消息语义，不写入会话持久历史。

CLI、TUI、REST、WebSocket 和 Mac Workbench 共用 `AgentEngine`，无需在各前端重复实现时间逻辑。

## Error Handling

默认系统时钟理论上总能返回本地时间。如果注入的测试或扩展时钟返回无时区 `datetime`，组装器将其解释为本机本地时间并转换为带时区值，确保模型永远不会收到缺少偏移的时间戳。

时区名称缺失时使用 `UTC<offset>` 作为显示名称。时间格式化不得访问网络，也不得触发权限确认。

## Tests

1. 使用固定的带时区时钟，断言 Harness 快照包含 ISO 时间、时区名称和 UTC 偏移。
2. 使用无时区固定时间，断言输出最终包含时区偏移。
3. 连续组装两次并返回不同时间，断言快照按轮刷新而非缓存旧值。
4. 保留现有 Harness 临时消息替换测试，确认快照不新增持久化副作用。
5. 运行 `tests/unit/test_context_assembly.py`、相关 engine 测试、Ruff 和真实组装器冒烟验证。

## Non-Goals

- 不新增 `current_time` Tool 或斜杠命令。
- 不从公网时间服务校时。
- 不改变系统时区或提供跨时区换算服务。
- 本次不修改 Mac Workbench 的通用工具权限确认流程；时间回答不应依赖该流程。

## Success Criteria

- 模型每轮都能看到带时区的当前本地时间。
- “现在几点”“今天几号”等问题无需工具即可回答。
- 新逻辑对所有共用 `AgentEngine` 的界面生效。
- 时间信息不会作为旧值持久化到会话历史。
- 定向测试、Ruff 和真实上下文组装验证全部通过。
