# ARC-04.5d2c Agent Result Inbox Projection

## 1. 用户问题与依赖结论

ARC-04.5d2b 已把 Agent 终态结果可靠投递到加密、幂等的 durable result inbox，但用户仍只能在当前
执行的内存时间线中看到回复。进程重启、Bus 通知丢失或切换到 Agent Control 后，没有统一的只读界面可以
证明哪些结果已经持久投递。

本切片只实现结果账本的安全投影：

```text
AgentJobStore result inbox
  -> SubAgentManager authenticated read boundary
  -> AgentControlService bounded/redacted projection
  -> Agent Control schema v3
  -> Python Bridge
  -> New UI 结果 tab
  -> Textual TUI 结果 tab
```

依赖审计结论：

- 不新建 Harness/Bus/UI 私有结果 Store；
- 不提前实现 ARC-04.6 Supervisor，因为 embedded Agent 尚不是独立长寿命 Worker；
- 不让前端直接读取 SQLite、解密 payload 或自行校验摘要；
- 复用 ARC-04.5d2b 的 session routing HMAC、delivery fence 与认证恢复；
- New UI 与 TUI 消费同一 Agent Control authority。

## 2. Runtime 读取边界

`SubAgentManager.list_result_inbox(session_id, limit=50)` 是 UI 可调用的最窄运行时端口：

1. 以当前 session ID 调用 Store 的 routing-HMAC inbox 索引；
2. 使用显式 `newest_first` 查询最多读取最新 50 条 delivery；Store 默认 FIFO 语义保持不变；
3. 每条使用精确 `delivery_id + delivery_sha256` 调用
   `recover_delivered_result()`；
4. Store 重新认证 delivery receipt、published receipt、AgentJob、request/result binding 与
   AES-GCM terminal payload；
5. 返回 delivery 与认证内容的不可变组合。

这个端口不暴露 Store 实例、claim owner、lease expiry、Runtime key 或数据库路径。
`publication_backlog()` 只暴露 content-free 的 pending/live-claimed/expired 计数。

## 3. Agent Control schema v3

schema 从 v2 升级到 v3，新增 required `results` section。结果条目是严格、闭集、有界的
`AgentResultDescriptor`：

- identity：`delivery_id`、`publication_id`、`job_id`、`task_id`、`agent_name`；
- terminal：`status`、`reason_code`、`delivered_at`；
- fence：`result_sha256`、`delivery_sha256`，必须是小写 SHA-256；
- public excerpts：`task_excerpt`、`response_excerpt`、`error_excerpt`；
- disclosure：`content_truncated`；
- usage：`response_bytes`、`total_tokens`、`total_cost_usd`、`turns`。

状态闭集为 `completed/error/timeout/max_turns/cancelled`。列表最多 50 条，每段公开文本最多
2000 字符；未知字段、错误类型、非法状态、空 identity 和错误摘要均 fail closed。

summary 新增：

- `durable_results_visible`：当前 session 当前快照可见条数，不冒充全量计数；
- `durable_publications_pending`；
- `durable_publications_claimed`；
- `durable_publications_expired`。

## 4. 脱敏、截断与会话隔离

Agent Control 不把已解密原文原样交给前端：

- task/response/error 先通过 `OutputGuardrail.redact()`；
- 每项再限制为 2000 字符；
- 发生脱敏或长度截断时，`content_truncated=true`；
- UI 明确提示“展示内容已经脱敏或截断”，不暗示这是完整原文；
- 原始结果继续只存在 ARC-04.5d1 的加密 terminal envelope；
- 每次 snapshot 只读取 Engine 当前 session 的 routing-HMAC inbox；
- session 切换后旧 results 立即从投影消失。

结果按 delivery 时间的最新优先顺序展示；摘要只来自 Store 已认证事实，前端不重算。

## 5. New UI 与 Textual TUI

两端 Agent Control 都新增只读“结果”标签：

- 列表显示终态颜色、task、Agent 和脱敏/截断标记；
- 详情显示状态、原因、时间、token/cost/turns、响应字节数及两类摘要；
- `completed` 使用成功色，错误/超时/轮数上限使用错误色，取消使用警告色；
- pending/claimed publication 使用警告色，expired claim 使用错误色；
- 空列表明确显示“当前会话暂无持久结果”；
- 结果标签不响应执行停止键，不提供 retry/ack/delete 等尚无权威后端的伪操作。

Python Bridge 继续使用现有 `agents/snapshot` / `agents/update`，`changed_sections` 可独立发送
`results`，不增加第二套 transport event。

## 6. 聚焦验收证据

- Python 严格模型 round-trip，并拒绝非法 SHA-256 与超过 50 条的结果；
- 真实 `AgentEngine + SubAgentManager + AgentJobStore` 完成一次 delegated Agent：
  - terminal payload 和 publication/inbox 正常落盘；
  - Agent Control 读取一条认证结果；
  - secret 不进入公开 excerpt；
  - 超长 response 截断到 2000 字符并标记；
  - token/cost/turns 与 digest 保持一致；
  - 切换 session 后结果为空；
- Agent Control 其他数据源失败时仍按既有部分可用语义工作；
- New UI protocol 严格校验 schema v3、results 和 summary；
- New UI state 使用 `delivery_id` 做稳定选择，结果 tab 不触发 stop；
- New UI 72/120 列结果详情不溢出；
- Textual formatter、tab 导航和真实 Screen 路径展示相同结果；
- Bridge 的 snapshot/update/session/stop 聚焦测试通过；
- 仅运行上述小模块测试，不运行全量测试。

## 7. 自我审视与未完成边界

本切片已把“结果可靠存在”变成用户可见事实，但没有完成：

- read/unread、ack cursor、跨设备同步或多消费者语义；
- inbox pagination、搜索、过滤和完整结果导出；
- retry/backoff、quarantine/dead-letter 与人工恢复动作；
- publication/inbox retention、GC、备份 root 与删除检测；
- 大输出按页读取；当前只展示 2000 字符安全摘录；
- 独立 Agent Worker、Supervisor、跨进程 drain/upgrade；
- 跨主机 sink、数据库复制或 exactly-once 声明。

下一步应重新检查 Harness、UI 与 Supervisor 文档依赖，优先实现消费现有权威事实的下一项用户能力；
不能因为 ARC-04.5d2 已连续推进，就线性把整个 Agent daemon/Supervisor 一次做完。
