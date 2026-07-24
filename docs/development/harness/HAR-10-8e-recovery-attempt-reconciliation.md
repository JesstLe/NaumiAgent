# HAR-10.8e Pursuit 恢复请求终态对账

## 目标

HAR-10.8d 已让每次 `/pursue resume` 先写入可复验的 `requested/admitted/terminal`
事件链，但仍存在一个真实崩溃窗口：模型循环已经写完后置 checkpoint 和机械边界裁判，
进程却在把 recovery attempt 写为 terminal 前中断。此时 attempt 会永久停留在
`admitted`，UI 也会持续阻止新的恢复请求。

本切片提供 `/pursue reconcile <recovery-attempt-id>` 和同源
`pursuit_reconcile` Agent Tool。它不会根据 UI 状态、中文消息或单个 run status
猜测完成，而是：

1. 复验 attempt 确实为 `admitted`，且已超过 30 秒安全宽限期；
2. 读取 Harness heartbeat 与 RunLease，拒绝健康执行者、有效租约和时钟回退；
3. 以唯一 owner 取得更高 RunLease epoch，并记录 accepted fence decision；
4. 在 `PursuitStore` 单一 `BEGIN IMMEDIATE` 事务中复验后置 checkpoint、机械裁判、
   run 指针和三方状态；
5. 原子追加 attempt `resolved` 事件并保存不可变对账回执；
6. 释放短期对账租约；释放失败不会撤销已持久的同库收口，但会显示租约到期提醒。

## 不变量

### 执行者存活判定

以下任一事实成立时不得进入 Store 收口事务：

- heartbeat 为 `starting/healthy/draining`；
- heartbeat 出现 `clock_regression` 或结构无效；
- RunLease 为 active 且尚未过期；
- 当前 lease epoch 早于 attempt 准入 epoch；
- heartbeat epoch 超前于 lease epoch；
- 原 lease 历史缺失；
- 更高 epoch 的 lease claim 或 fence decision 未被接受。

`stale/offline/stopped/failed/missing` heartbeat 本身不授权收口。只有更高 epoch
RunLease claim 与 accepted fence decision 才能形成机械 fencing。

### 同库终态证据

Store 事务只接受以下组合：

- attempt 状态为 `admitted`，且 `lease_epoch > 0`；
- 当前 boundary decision 由 run 的内容寻址指针引用；
- boundary decision `recorded_at > admitted_at`；
- 当前 checkpoint 内容与摘要有效，`created_at > admitted_at`；
- run、checkpoint、boundary decision 的时间均不晚于本次 reconcile，未来证据失败关闭；
- 后置 checkpoint ID 不等于 admission 记录中的 checkpoint ID；
- decision status 不是 `running`；
- decision、run、checkpoint 状态严格相等：
  - `waiting -> waiting`
  - `blocked -> blocked`
  - `completed -> completed`
  - `cancelled -> cancelled`
  - `budget_exceeded -> budget_exceeded`
- run 更新时间不早于 boundary decision，checkpoint 创建时间不早于 boundary decision；这与生产路径
  “先持久化裁判/run，再构建 checkpoint”的顺序一致。

`failed` 暂无同构的机械 boundary status，不能自动对账；证据缺失或不一致时 attempt
保持 `admitted`。

## 持久回执

`PursuitRecoveryReconciliationReceipt` 是冻结、schema v1、内容寻址的不可变事实：

- attempt 收口前后 payload SHA-256；
- admission 与 fencing lease epoch；
- Harness fence operation ID；
- 后置 checkpoint ID 和创建时间；
- boundary decision ID、记录时间与结果码；
- admitted/reconciled 时间。

`receipt_id = precon-<sha256(canonical facts)>`。表
`pursuit_recovery_reconciliations` 每个 attempt 只能保存一条回执。读取时同时验证：

- 回执 payload 摘要与 metadata；
- receipt ID 与 canonical facts；
- 当前 attempt 必须是 `resolved`；
- attempt digest、result code、boundary decision 必须与回执一致。

并发或重启后的重复请求返回同一回执，不会追加第四个 attempt event。

## 双通道与界面

- 用户通道：`/pursue reconcile <attempt-id>`；
- Agent 通道：`pursuit_reconcile(attempt_id=...)`；
- CLI、New UI 共享斜杠路由，Textual TUI fallback 使用同一 ToolExecution；
- `/pursue status`、New UI Goal 页面和 TUI 文本投影会在 `admitted` 记录旁展示同一
  reconcile 命令；
- bypass 直接允许执行，但仍保存持久权限决策、Harness fence 和对账回执；
- 默认模式仍按现有工具权限政策处理，不由 renderer 绕过授权。

## 验收标准

- 健康 heartbeat 或有效 lease 下，对账不 claim 新 epoch、不修改 attempt；
- expired/released lease 下，真实 HarnessStore claim 推进 epoch 后才能收口；
- 没有后置机械裁判或 checkpoint 时，attempt 保持 admitted；
- admission 前的裁判/checkpoint、同一 checkpoint、三方状态不一致全部失败关闭；
- 成功对账恰好产生 sequence 3 resolved event 和一条 immutable receipt；
- 16 路 Store 重试只得到一个 receipt ID、一条回执和三条 attempt events；
- 回执摘要、metadata 或 attempt 终态被篡改时读取失败；
- 第一次 fencing 后证据仍缺失，可以在证据补齐后用新 epoch/new operation 重试；
- Store 重开后回执和 attempt 终态仍可相互复验；
- 工具、CLI、New UI 与 TUI 的用户文案为中文，不显示 owner identity 或请求摘要；
- 非法 attempt ID 在触碰 Harness 或 Store 之前拒绝。

## 定向验证

```bash
ruff check \
  src/naumi_agent/orchestrator/pursuit_recovery_reconcile.py \
  src/naumi_agent/orchestrator/pursuit_recovery_attempt.py \
  src/naumi_agent/orchestrator/pursuit_store.py \
  src/naumi_agent/orchestrator/pursuit.py \
  src/naumi_agent/tools/pursuit.py \
  src/naumi_agent/main.py \
  src/naumi_agent/tui/app.py \
  src/naumi_agent/ui/goal_panel.py \
  tests/unit/test_pursuit_recovery_reconcile.py

pytest -q \
  tests/unit/test_pursuit_recovery_reconcile.py \
  tests/unit/test_pursuit_recovery_attempt.py \
  tests/unit/test_main_pursue_dispatch.py \
  tests/unit/test_permissions.py

node --test frontend/terminal-ui/test/goal-pursuit-page.test.js
```

本切片不运行全量测试。

## 自我审视与未完成项

- PursuitStore 与 HarnessStore 仍是两个 SQLite 事务域，无法宣称跨 Store 原子提交。本切片通过
  “先取得更高 epoch fencing，再执行同库原子收口”避免旧执行者继续拥有写入权；Harness
  fence decision 是跨域审计证据。
- 若 Store 收口成功后释放对账租约失败，短租约最长 30 秒后失效，回执会明确显示提醒；不会回滚已经
  验证完成的 attempt 终态。
- 本切片是显式、单 attempt 对账，不做无界启动扫描或自动批量修复。后续 outbox worker 必须使用
  cursor、scan limit、退避和同一 fencing 规则。
- 未执行真实 kill-at-every-write-point 矩阵、24 小时 soak 或多主机时钟漂移实验，HAR-10 仍为
  partial。
