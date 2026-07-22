# ARC-03.2b2 Session List Payload Boundary

## 目标

为 UI-14 Session QuickOpen 提供一个真实、只读、可关联的会话列表协议，避免新 UI 解析
`/load` 的自然语言输出，也避免当前工作区看到其他项目的会话标题。

本切片只实现 `sessions/list/request → sessions/list` v1，不实现 QuickOpen 渲染、选择或恢复动作。

## 权威边界

- Client 请求字段仅允许 `page`、`page_size`、`query`；工作区路径不能由客户端指定。
- Bridge 总是使用启动时解析后的 `engine.workspace_root`。
- `SessionStore.list_sessions(..., workspace_root=...)` 在 SQL 查询和总数统计阶段执行精确作用域，
  不采用“先取全局一页再过滤”的不完整实现。
- 旧 `/history` 与 API 未提供作用域时仍保持全局历史行为。
- `request_id` 原样关联请求与响应；读取失败返回固定的 `session_list_failed`，异常细节只进日志。

## v1 公共字段

响应包含 `schema_version`、`generated_at`、固定 `scope=workspace`、分页、总数、查询词、
有界 items 与 warnings。单项只投影：

- `session_id`：可安全用于 `/load` 的稳定标识；
- `title`、`model`、`updated_at`、`git_branch`；
- `message_count`、`user_message_count`；
- `is_current`、`resumable`。

消息正文、summary、workspace 原始路径、Token、费用和任何私有扩展字段均不进入协议。
Node 接收端重新执行 schema、作用域、分页、ID、类型和字符上限校验，并通过白名单重建对象。

## 限界

- `page`: 1..10000；`page_size`: 1..100；`query`: 0..200 字符。
- items 最多 100；warning 最多 10 项、单项最多 300 字符。
- title 300、model 200、branch 200、timestamp 64、session ID 128 字符。
- 非法 session ID 不向客户端暴露，并返回不含原始 ID 的计数警告。

## 验收证据

- SQLite 真实存储验证精确工作区过滤与匹配总数。
- Python 投影测试验证 current/resumable 事实及 message/summary/path 不泄露。
- Bridge 测试验证请求关联和固定错误脱敏。
- Python client payload 测试验证分页、搜索词限界且客户端路径字段被丢弃。
- Node 协议测试验证 100 项上限、严格类型/作用域/ID 与私有字段投影。
- 相关文件通过定向 Ruff；未运行全量测试。

## 未完成项

- UI-14.2d 才会把该 snapshot 接入 New UI/TUI Session QuickOpen，并由用户选择填充
  `/load <id>`，本切片不提前声称用户入口已完成。
- ARC-03 的通用 JSON Schema、兼容矩阵、ordering/gap recovery、代码生成与完整 conformance
  suite 仍为 planned。
