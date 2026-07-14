# 持久目标 `/goal` 设计

日期：2026-07-14

## 1. 目标

为 NaumiAgent 增加一个宿主级、跨对话轮次可见的当前目标。目标由用户显式创建，普通聊天不必立即启动长循环；Engine 每轮把未完成目标注入可信 Harness 快照，模型可以围绕同一目标持续工作。

支持：

- `/goal <目标>` 或 `/goal create <目标>`：创建当前目标；
- `/goal`：查看当前目标；
- `/goal list`：查看历史；
- `/goal pause [说明]`、`resume [说明]`、`block <原因>`、`complete [说明]`、`cancel [说明]`；
- `/goal pursue`：用当前目标启动既有 Pursuit 自主循环，并记录关联 run_id；
- Agent 工具与 CLI/TUI/New UI 命令共用同一 SQLite store。

## 2. `/goal` 与 `/pursue` 的边界

### 方案 A：`/goal` 只是 `/pursue` 别名

会让“记住目标”和“立即自主执行”无法分离，也无法自然暂停、恢复或在普通对话中继续推进。不采用。

### 方案 B：为 `/goal` 再建一套自主循环

会重复 Pursuit 的规划、工具执行、证据和恢复机制，两个循环也可能竞争修改同一工作区。不采用。

### 方案 C：持久目标登记层 + 复用 Pursuit

`/goal` 管理长期意图与生命周期；`/pursue` 管理一次自主执行运行。`/goal pursue` 通过受权限控制的 `goal_pursue` 工具调用既有 Pursuit 实现，并把 run_id 关联到目标。采用。

## 3. 数据模型与一致性

数据库位于 Engine 工作区运行目录下的 `goals/goals.db`，与 Pursuit 数据同属 `.naumi` 运行数据，不写入用户全局配置。

字段：`id`、`objective`、`status`、`note`、`session_id`、`pursuit_run_id`、`created_at`、`updated_at`。

状态：`active`、`paused`、`blocked`、`completed`、`cancelled`。前三者属于未完成状态；后两者是终态。SQLite 部分唯一索引保证一个工作区最多只有一个未完成目标，即使并发创建也不会产生两个当前目标。

允许的状态转移：

- active → paused / blocked / completed / cancelled；
- paused → active / blocked / completed / cancelled；
- blocked → active / paused / completed / cancelled；
- completed / cancelled 不可再次变更。

目标、说明和 ID 统一校验长度与控制字符；错误返回中文、可操作信息。

## 4. Agent 工具

- `goal_create`：显式创建目标；
- `goal_status`：读取当前目标或指定目标；
- `goal_list`：列出目标历史；
- `goal_update`：执行合法状态转移；
- `goal_pursue`：读取未完成目标，调用既有 `pursue_goal`，提取并保存 run_id。

写工具标记为 destructive 并走 Engine 权限执行器；读取工具为 concurrency-safe。`goal_create` 的描述明确：只有用户显式要求创建长期目标时才可调用，避免模型擅自把普通消息升级为持久目标。

## 5. 上下文注入

Harness 快照新增 `### 当前目标`：

- 没有未完成目标时明确显示“当前没有未完成目标”；
- 有目标时包含 ID、状态、目标、说明、关联 pursuit run；
- 只进入每轮临时 `_messages` 快照，不写入 `_full_history`，避免陈旧状态污染会话记录。

## 6. UI 与命令同步

共享 `_handle_command` 实现 `/goal`，因此旧 CLI、Textual TUI 和 New UI 走同一后端。补全命令元数据、帮助、自动补全和 New UI slash command 目录；所有可见文案中文优先。

## 7. 测试

只运行相关小模块：

- Store：持久化、唯一未完成目标、并发创建、合法/非法转移、终态；
- Tools：创建、状态、更新、Pursuit 复用及 run_id 关联、错误路径；
- Slash：各子命令经 Engine `_execute_tool`，不绕过权限；
- Context：当前目标进入临时 Harness 快照；
- UI bridge：`/goal` 进入共享命令目录和分发；
- Ruff、py_compile、git diff --check。

## 8. 自我审视

- 目标不自动恢复旧 UI 草稿或侧栏；它是用户显式创建的工作区意图，与界面临时状态无关。
- 本功能不自动循环继续执行。若用户只创建目标，Agent 仅在后续轮次得到上下文；要无人值守推进需明确 `/goal pursue`。
- 一个工作区只允许一个未完成目标，避免模型在多个长期意图之间隐式切换；需要新目标时先完成或取消当前目标。
- Goal 关联 Pursuit，但不复制 Pursuit 的证据。运行详情仍以 `/pursue status <run_id>` 为准，避免双写状态漂移。
