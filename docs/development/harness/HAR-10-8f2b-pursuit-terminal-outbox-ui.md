# HAR-10.8f2b Pursuit 终态 Outbox 可观测投影

## 目标

HAR-10.8f2a 已让 terminal outbox 自动恢复，但 worker 与队列状态只存在于 Python runtime 和
PursuitStore。用户无法判断“没有动作”究竟是队列为空、正在认领、等待退避、worker 已关闭，还是
authority 读取失败。本切片把同一份有界事实投影到：

- Bridge `goals/snapshot`；
- New UI Goal / Pursuit 页面；
- Goal Tool 输出，即 CLI/Textual TUI fallback。

前端不读取 SQLite、不推断 lease/outbox 状态，也不接收 claim owner 摘要或 outbox 标识。

## 协议

`goals/snapshot` 保持 schema v2，新增可选的 `terminal_outbox` schema v1。采用可选嵌套字段而不是
强制把根协议升级到 v3：旧客户端会忽略新增字段，新客户端仍可与未提供该字段的旧后端协作。

```json
{
  "terminal_outbox": {
    "schema_version": 1,
    "enabled": true,
    "status": "recovering",
    "worker_state": "waiting",
    "assessed_at": "2026-08-05T00:00:20+00:00",
    "counts": {
      "total_pending": 3,
      "due": 1,
      "backoff": 1,
      "live_claimed": 1,
      "expired_claimed": 0
    },
    "pass_count": 7,
    "delivered_count": 2,
    "retry_scheduled_count": 1,
    "failure_count": 0,
    "next_delay_seconds": 12.5,
    "failure_codes": [],
    "warning": ""
  }
}
```

状态语义：

- `idle`：已启用、authority 可读且队列为空；
- `recovering`：存在 due、live claim 或 expired claim；
- `backoff`：待处理记录全部在持久退避窗口；
- `degraded`：最近一轮存在类型化 failure code；
- `disabled`：配置明确关闭；
- `unavailable`：队列或 worker authority 无法读取，失败关闭而非显示为空。

## 权威与隐私边界

- backlog 来自 `PursuitStore.terminal_outbox_backlog()` 的最多 10000 条认证扫描；
- worker 状态来自 `PursuitTerminalOutboxWorker.snapshot()`；
- Bridge 与 Goal Tool 只传 getter，不复制 worker 状态机；
- 五个分类计数之和必须严格等于 `total_pending`；
- New UI 再次严格校验 schema、枚举、有限数值、计数和 failure code；
- 不投影 `outbox_id`、`attempt_id`、`claim_owner_sha256`、owner credential 或 payload digest；
- authority 缺失/异常显示中文 warning 和 `unavailable`，不得伪装为零积压。

空工作区不会为了展示零值而创建 Pursuit 数据库；只有数据库已经存在时才读取 backlog。

## 三端体验

### New UI

Goal 页面新增“终态自动恢复”区块，使用颜色区分：

- 绿色：空闲；
- 青色：正在恢复；
- 黄色：等待重试或配置关闭；
- 红色：部分失败或状态不可用。

页面展示队列分类、累计轮次/收口/退避/失败、下次检查时间和最近 failure code。即使没有 Goal，
全局恢复服务仍可见。

### CLI / Textual TUI fallback

`goal_status` 与 `goal_list` 复用相同 `GoalPursuitSnapshot`，渲染相同字段和中文状态，不另行扫描 Store。
因此 `/goal` 在 TUI fallback 中能看到同源队列/worker 事实。

## 验收标准

- 真实 Engine 将 enabled 配置和 worker snapshot getter注入 Goal Tool 与 Bridge；
- backlog 分类和 worker metrics 同时进入 typed payload 与 Markdown fallback；
- authority 缺失时输出 `unavailable`，并且空工作区不创建数据库；
- 协议拒绝计数和不一致、非法状态、非有限 delay、超长 warning 和非法 failure code；
- New UI 在无 Goal 时仍显示 terminal outbox 服务；
- New UI 不保留嵌套对象中的未知/private owner 字段；
- Goal Tool/TUI fallback 与 New UI 使用相同中文语义；
- 仅运行 Goal、Bridge Goal、New UI protocol/render 小模块，不运行全量测试。

## 自我审视与未完成项

- 本切片是读取投影，不提供 pause、wake、retry-now、dead-letter 或 prune 操作；这些动作需要独立权限、
  fencing 与不可变回执设计。
- 当前是请求时 snapshot，不是 push stream；Goal 页通过 `r` 刷新。专用增量事件需先定义 revision/cursor。
- 10000 条以上 backlog 失败关闭，不提供历史分页；cursor/retention 属于后续治理。
- 尚未完成 kill-at-every-write-point、跨主机时钟漂移、三平台进程杀死和 24 小时 soak。
