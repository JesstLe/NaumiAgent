# HAR-10.8d Pursuit 恢复请求审计账本

## 目标

HAR-10.8c 已让恢复路径使用统一机械边界裁判，但用户发起 `/pursue resume` 后，仍缺少一个独立、
持久、可去重的请求事实。网络重试、重复 ToolCall、并发恢复或进程中断时，界面只能看到运行最终状态，
不能回答“这一恢复请求是否已记录、是否取得准入、最终由哪个机械边界关闭”。

本切片在 `PursuitStore` 内建立内容寻址的 recovery attempt 事件链，并把生产
`pursuit_resume` ToolCall 的持久权限回执作为请求身份来源。它是 UI-18.5b1 恢复动作的最小前置，
不在本切片提前实现前端按钮。

## 权威模型

`PursuitRecoveryAttempt` 是严格、冻结、schema v1 的状态记录：

| 状态 | sequence | 必需事实 | 禁止事实 |
|---|---:|---|---|
| `requested` | 1 | run、请求摘要、请求时间 | lease、checkpoint、结果 |
| `admitted` | 2 | 准入时间、checkpoint、lease epoch | 结果、机械裁判 |
| `resolved` | 2 或 3 | 结果码、关闭时间 | 不适用 |
| `failed` | 2 或 3 | 失败码、关闭时间 | 机械裁判引用 |

未取得准入的请求从 sequence 1 直接进入 sequence 2 终态；取得准入后只能进入 sequence 3 终态。
terminal 记录不可重写。embedded fallback 没有外部 lease 时显式记录 `lease_epoch=0`，但仍必须绑定
已验证 checkpoint；它不能伪装成 durable lease。

`attempt_id` 由 `run_id + SHA-256(source_request_id)` 计算，模型会再次按保存的请求摘要复算 ID，
防止数据库内的 identity 与请求事实分离。Store 不保存原始 ToolCall ID、用户文本或权限参数。
请求身份最多 512 字符，避免异常调用制造无界哈希输入。

## 生产请求身份

`pursuit_resume` 声明 `requires_persistent_authorization=True`：

1. Engine 在 policy allow 或 bypass 下，都必须先持久化 `PermissionDecisionReceipt`；
2. Tool 只从 task-local permission context 读取该回执的 `call_id`；
3. `call_id` 只用于本地计算请求摘要和 attempt ID，不写入 recovery payload；
4. 同一个 ToolCall 重试得到同一个 attempt，新的用户动作使用新的 ToolCall ID；
5. 脱离 Engine 的直接工具调用使用随机本地身份，避免不同手动调用意外合并。

如果权限回执不能持久化，Engine 在工具执行前失败关闭，因此不会出现“执行过恢复但没有请求身份”的
生产路径。bypass 表示直接允许执行，不跳过持久审计。工具回执只在从 PursuitStore 复验到 attempt 后
显示其 ID；账本写入失败时不得展示一个仅由调用端预测、实际并不存在的 attempt。

## 状态迁移顺序

生产恢复顺序固定为：

1. 验证 run 存在；
2. 在取得 Pursuit operation lock 和 run lease 前原子写入 `requested`；
3. 重复请求直接返回既有状态，不再次取得 lease 或调用模型；
4. operation busy、无 checkpoint、终态、lease unavailable 等未准入结果直接 `resolved`；
5. 只有恢复 checkpoint 已验证、running 边界已持久化后，才写入 `admitted`；
6. `admitted` 必须在 `_resume_admitted_event` 和模型循环之前落盘；
7. 执行结束使用类型化 `message/result_code/boundary_decision_id` 关闭账本，禁止解析中文展示文本推断结果；
8. 只有 result code、decision ID 与当前 `PursuitRun.boundary_decision` 同时一致，才保存机械裁判引用；
9. cancellation 与非预期异常进入 `failed`，不伪造机械裁判。

`requested`、`admitted` 和 terminal 都写入 append-only 事件表。每个事件保存当前 payload 摘要和前一
payload 摘要；快照必须与事件链末端、sequence、state、run 全部一致，否则读取失败关闭。

## 用户可见投影

`/pursue status <run-id>` 在原有运行信息后显示最近 5 条恢复请求：

- 不透明 attempt ID；
- 中文状态与稳定结果码；
- 更新时间；
- admitted checkpoint 的短摘要和 lease epoch/embedded fallback；
- 经过复验的机械裁判短 ID。

投影不显示 source request digest、权限回执内容、owner identity 或原始错误正文。列表有 Store 端
`1..200` 硬上限；当前状态命令固定读取 5 条。

## 验收标准

- 相同 `(run_id, ToolCall.id)` 在并发和重启后只创建一个 attempt；
- 不同 ToolCall 不会意外合并；
- attempt ID 与保存的 run/request digest 内容寻址一致；
- recovery attempt payload、公开格式化结果和 Pursuit 事件表都不包含原始 ToolCall ID；权限回执仍按
  既有审计合同保存 call ID；
- 16 路并发 prepare 恰好一方创建，其余读取同一记录；
- `requested` 必须早于 operation lock/lease，`admitted` 必须早于模型循环；
- 缺失 checkpoint 的请求以 `checkpoint_required` sequence 2 关闭并引用同一机械裁判；
- 安全 resume 先进入 sequence 2 admitted，再进入 sequence 3 resolved；
- 同一请求在 admitted 期间重试只返回现状，不重启循环；
- cancellation/internal error 使用 failed，且没有 boundary decision ID；
- terminal 冲突重写、缺失 run、事件断链、快照篡改和 identity 不一致全部失败关闭；
- Store 重开后可复验完整事件链；
- policy/bypass 都通过持久权限回执把 ToolCall ID 传给工具；
- 工具只展示 Store 已复验的 attempt ID，账本失败不得产生虚假回执；
- 状态输出使用中文标签，并隐藏请求摘要。

## 定向验证

```bash
ruff check \
  src/naumi_agent/orchestrator/pursuit_recovery_attempt.py \
  src/naumi_agent/orchestrator/pursuit_store.py \
  src/naumi_agent/orchestrator/pursuit.py \
  src/naumi_agent/tools/pursuit.py \
  tests/unit/test_pursuit_recovery_attempt.py \
  tests/unit/test_pursuit_checkpoint.py \
  tests/unit/test_pursuit.py

python3 -m compileall -q \
  src/naumi_agent/orchestrator/pursuit_recovery_attempt.py \
  src/naumi_agent/orchestrator/pursuit_store.py \
  src/naumi_agent/orchestrator/pursuit.py \
  src/naumi_agent/tools/pursuit.py

pytest -q \
  tests/unit/test_pursuit_recovery_attempt.py \
  tests/unit/test_pursuit_checkpoint.py \
  tests/unit/test_pursuit_lease.py \
  tests/unit/test_pursuit.py \
  tests/unit/test_main_pursue_dispatch.py
```

本切片只运行 Pursuit/Store/lease/Tool 小模块，不运行全量测试。

## 自我审视与未完成项

- recovery attempt 与 PursuitRun/checkpoint/boundary decision 位于同一个 `PursuitStore` SQLite，但
  Harness lease、heartbeat、interaction 和 BackgroundTask 仍属于其他事务域；本切片不宣称跨 Store
  exactly-once。
- 外部副作用完成后若 terminal attempt 写入本身失败，记录可能停留在 admitted。HAR-10.8e 已提供
  显式、fenced 的单 attempt reconciliation authority：先推进 Harness RunLease epoch，再按同库
  后置 checkpoint/机械裁判收口并保存不可变回执；后续仍需 bounded outbox worker。
- run 不存在时没有可满足外键的 recovery attempt；调用会在账本创建前返回明确错误。
- 当前提供最近记录，不提供 cursor、retention 或跨 run catalog；这些应随 UI-18.5 的恢复历史设计推进。
- 没有执行 A5 24 小时 soak、进程 kill 或跨平台故障矩阵，HAR-10 仍为 partial。
- UI-18.5b1 已消费此 authority：New UI 的 typed resume 重新读取 Python 准入状态、通过
  ToolExecution 发起并显示 requested/admitted/terminal 回执；TUI fallback 展示同源动作和共享命令。
  前端不生成结果状态。后续仍需 terminal push/outbox、历史 cursor/retention 和 takeover/cleanup
  的独立治理。
