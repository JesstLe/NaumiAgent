# 子智能体活动可视化设计

日期：2026-07-14

## 1. 目标

把子智能体生命周期从零散的 `subagent_event` 行升级为稳定、可读、可折叠的任务卡片。用户应能在主时间线直接看到：谁在做什么、当前状态、最新进展、耗时与资源；需要细节时再展开事件历史。

## 2. 现状与问题

后端每个子任务通常发送 started 与 completed/failed 两条事件，字段包含 task_id、agent_name、description、message、tokens、cost、timestamp。UI Adapter 目前丢弃 description、tokens、cost、timestamp；New UI 为每条事件创建独立通用卡片，Textual TUI 只打印单行，导致上下文割裂，也无法像参考图一样展开查看。

## 3. 方案

### 方案 A：只美化每条事件

改动小，但同一任务仍会出现多张卡片，无法形成任务级认知。不采用。

### 方案 B：前端按 task_id 聚合

保留现有后端协议，在 typed UI message 中补齐字段。New UI 收到事件后，按 task_id 更新同一个 `subagent_activity` 模型；新一轮同 ID 的 started 事件在旧任务终态后创建新卡片。采用。

### 方案 C：后端维护 UI 卡片状态

会把展示状态耦合到执行引擎，并增加恢复与版本迁移负担。不采用。

## 4. New UI 卡片

折叠态：

```text
+ 子智能体 -----------------------------------------------------+
| ▸ 进行中 · Explore · 探索项目结构与模块实现                    |
+----------------------------------------------------------------+
```

展开态补充：task ID、完整任务描述、最新消息、开始/更新时间、耗时、token、费用和最近事件。颜色语义：运行中 cyan、完成 green、失败/error red、取消 yellow。

卡片默认折叠并注册到现有 `/folds`、`/expand`、`/collapse` 系统；展开是 UI 本地状态，不写入后端会话。窄终端必须安全换行，所有文本经过终端字符清理和长度上限。

## 5. 聚合规则

- task_id 为主键；缺失时使用 agent_name 与消息 ID 作为降级键；
- started/running 优先更新未终态卡片；终态后的新 started 创建新卡片；
- 单卡最多保留 20 条最近事件，文本设长度上限；
- started timestamp 作为开始时间，最后事件作为更新时间；后端无时间时用前端接收时间；
- terminal 状态：completed、failed、error、cancelled；
- 不把子智能体卡片混入普通 assistant 文本，也不重复创建通用 EventCard。

## 6. Textual TUI 同步

typed `SubagentEventMessage` 同步携带全部公开字段。TUI 用中文状态、任务描述、消息、token、费用呈现两层信息；保持 renderer 无状态，避免在旧 fallback 中引入另一套持久聚合器。任务级完整历史仍可通过 `/agents` 与 `/tasks` 查看。

## 7. 测试

- Adapter：字段不丢失、缺失字段安全默认；
- Node state：同 task 聚合、新一轮同 ID、事件上限、终态；
- Node component：折叠/展开、颜色、窄宽终端、CJK、资源格式；
- folds：子智能体卡片出现在 `/folds`；
- TUI renderer：中文状态与详细字段；
- protocol、node --check、Ruff、py_compile、git diff --check。

## 8. 自我审视

- 当前后端 completed 事件只说“已完成”，实际产物仍由父 Agent 回复或 Agent 控制中心展示；卡片不伪造不存在的结果摘要。
- 前端聚合以 task_id 为边界，没有 backend run_id 时只能以“终态后新的 started”判断复用 ID 的新一轮，这是现有协议下的最可靠规则。
- Textual fallback 保持事件级渲染，不与 New UI 复制复杂聚合状态；但字段与颜色语义一致。
