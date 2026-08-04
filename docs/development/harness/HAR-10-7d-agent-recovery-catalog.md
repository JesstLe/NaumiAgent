# HAR-10.7d Agent Recovery Catalog

## 1. 目标

HAR-10.7c 已把 embedded Agent 的 active/waiting 容量升级为跨 Runtime 的 durable authority，
ARC-04.5d2a/2b 又建立 terminal publication outbox 与 result inbox。但用户此前只能看到聚合计数，
不能区分一个占用容量的 Job 仍在健康运行、已可安全接管、需要人工裁决，还是已经进入 unknown。

本切片交付一个只读、严格有界、逐条认证的恢复目录，并通过同一 Agent Control authority 同步到
New UI 与 Textual TUI。它不增加第二套 Agent 状态机，不自动重放模型调用，也不提供持久状态改写。

## 2. 范围

### 2.1 已实现

- `AgentJobStore.recovery_catalog(limit=50)` 在同一个只读事务中读取：
  - `claimed`、`running`、`unknown` Job；
  - `pending` publication；
  - claim 已过期的 publication。
- 每类最多读取 100 项，Agent Control 最终最多投影 50 项，并显式返回 `truncated`。
- Job 通过 payload envelope、request binding、latest receipt、HMAC 和完整事件链恢复。
- publication 同时认证 publication receipt、其 terminal Job 和二者绑定关系。
- Agent Control schema v4 增加 `recovery_catalog` section，New UI/TUI 增加“恢复”标签。
- 目录按恢复风险排序：
  1. `recovery_required`；
  2. `outcome_unknown`；
  3. `publication_claim_expired`；
  4. `reclaimable_prestart`；
  5. `publication_pending`；
  6. `worker_active`；
  7. `claim_active`。

### 2.2 明确不实现

- 自动重放 Agent 模型请求；
- 自动把 expired running Job 收口为 `unknown`；
- UI takeover、retry、cancel、ack 或 cleanup 动作；
- publication 周期 worker、dead-letter 或 retention；
- 跨主机 Agent Worker dispatch。

这些动作必须在后续切片中绑定最新 receipt digest、claim epoch 和明确副作用证据，不能由前端根据
展示状态猜测。

## 3. 权威边界

```text
AgentJob SQLite
  -> authenticated recovery catalog
  -> SubAgentManager narrow read port
  -> AgentControlService schema v4
  -> Bridge agents/snapshot | agents/update
  -> New UI / Textual TUI
```

只有 Store 判断持久事实是否可信；manager 不解密正文，Agent Control 不直接查询 SQLite，前端只校验
并渲染 schema v4。`assessed_at` 只表示本轮分类时刻，不参与 revision fingerprint，避免每次刷新产生
虚假增量。

## 4. 状态语义

| recovery_state | 权威事实 | 用户含义 | 当前动作 |
|---|---|---|---|
| `claim_active` | claimed 且 lease 未到期 | 已认领，尚未进入模型调用 | 只读等待 |
| `worker_active` | running 且 lease 未到期 | worker 仍在执行 | 只读等待 |
| `reclaimable_prestart` | claimed 且 lease 已过期 | 尚未 start，可由 scheduler 安全接管 | 本切片不操作 |
| `recovery_required` | running 且 lease 已过期 | 副作用可能已经发生，需要恢复裁决 | 本切片不操作 |
| `outcome_unknown` | Job 已被 fenced 为 unknown | 无法证明成功或安全重试 | 人工审查证据 |
| `publication_pending` | terminal publication 尚未 claim | 结果等待投递 | startup worker 可处理 |
| `publication_claim_expired` | publication claim 已过期 | 投递 worker 中断，可被接管 | startup worker 可处理 |

`session_scope` 只比较 request 中已有的 session SHA-256 与当前 session SHA-256，输出
`current/other/unknown`，不解密或公开原始 session ID。

## 5. 公开字段与隐私

允许投影：kind、稳定 item/job/publication ID、Agent 名、Job/recovery 状态、会话范围、claim epoch、
claim expiry、publication attempt count、发生时间、request/receipt SHA-256 和稳定 reason code。

禁止投影：owner ID、原始 task/context/response/error、payload envelope、凭据、工作区绝对路径、模型
reasoning、消息正文和解密后的 session ID。New UI 协议 normalizer 会重建白名单对象，额外字段不会进入
渲染状态。

## 6. UI 行为

- New UI 和 Textual TUI 都提供“恢复”标签；红色表示需要裁决或未知，黄色表示可接管/待投递，蓝色
  表示 live 状态。
- 列表以稳定 `recovery:<kind>:<item_id>` 作为选择身份，刷新后不依赖显示文本定位。
- 详情显示短摘要和精确状态，不显示 owner 或正文。
- 页面明确提示“只读、不自动模型重放、不改写持久状态”。
- 超过 50 项时显示截断警告，不能把未展示条目解释为不存在。

## 7. 失败与并发语义

- 任一候选 Job、publication 或绑定认证失败，整个 Store catalog fail closed，不返回部分可信目录。
- Agent Control 将 catalog 失败隔离为 warning，其他 Agent/执行/结果/团队 section 继续可用。
- SQLite 查询有明确 `LIMIT`；不存在无界恢复扫描。
- `assessed_at` 与同一批查询共用一个 aware UTC 时刻，避免同一快照内把相同 expiry 分到不同状态。
- 目录是观察路径，不 claim、不 renew、不推进 epoch，不与生产 worker 竞争。

## 8. 验收证据

- 真实 SQLite 创建 live claim、expired pre-start claim、expired running、unknown 和 pending publication，
  catalog 能在重开后逐条认证并保持正文不出现在 repr。
- `limit=2` 返回两个 Job 且 `jobs_truncated=true`；publication 截断独立计算。
- 篡改候选 Job latest receipt 后，catalog 以 `AgentJobError` 拒绝整个读取。
- Agent Control 对真实运行中的 embedded Agent 投影 `worker_active/current`，连续刷新不因
  `assessed_at` 单独增加 revision。
- Python model 拒绝错误 kind/ID/state/digest/timestamp 和超过 50 项的 payload。
- New UI protocol 拒绝错误摘要与交叉类型身份，不保留 `owner_id`。
- New UI 120/72 列与 Textual formatter 都显示只读恢复详情且不超宽。

## 9. 后续依赖

下一步不应直接把按钮接到 `mark_recovery_unknown()`。应先在 HAR-10、ARC-06 和 Supervisor 文档间
重新比较依赖，最小候选是：

1. exact fenced manual recovery action：必须携带最新 receipt SHA-256 与预期 epoch；
2. Agent publication 周期 retry 与 shutdown drain health；
3. 独立 Agent Worker dispatch/scheduler；
4. recovery catalog cursor/retention，仅在真实规模证明 50 项前缀不足后实现。
