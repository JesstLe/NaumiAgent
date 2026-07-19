# HAR-10.2a Typed Heartbeat Authority

## 目标

为长周期执行建立跨重启可读取、无需解析 UI 文本的 typed heartbeat authority，并完成第一个真实生产接入：
Pursuit lease worker。本切片不把 Bridge ping、Workbench agent 时间戳或进程内 subagent age 冒充为统一心跳，
也不一次性接入 browser/agent/runtime。

## 合同与职责边界

Heartbeat snapshot 持久化到 Harness DB v12，主键是
`workspace_root + subject_kind + subject_id`，包含：

- `instance_id`：实际执行实例身份；
- `epoch`：来自所属 authority 的单调所有权代次；
- `sequence`：同 instance/epoch 内单调递增；
- `phase`：starting/running/waiting/draining/stopped/failed；
- `observed_at`：包含时区的权威观测时间；
- `timeout_seconds`：该 producer 声明的陈旧阈值；
- `detail_code`：有限、无 secret 的机械原因码。

Lease 与 heartbeat 不可互换：lease 决定谁有权提交，heartbeat 只证明某实例最近报告了什么。Heartbeat 健康也
不能单独授权 takeover、重试或终止进程。

## 单调写入

- 完全相同的 heartbeat 是幂等 replay；
- 同 epoch 必须保持 instance_id，且 sequence 严格递增；
- 更低 epoch 被拒绝，避免旧 owner 覆盖新 owner；
- 更高 epoch 可从 sequence 1 开始；
- `observed_at` 不能倒退，即使 epoch 增长也不能隐藏时钟回退；
- workspace 隔离，非法 ID、phase、sequence、epoch、timeout 和无时区时间在写入前拒绝。

## 健康判定

`assess_heartbeat()` 是无副作用机械分类：

| 条件 | health |
| --- | --- |
| 当前时间早于 observed_at | clock_regression |
| phase stopped/failed | stopped/failed |
| age > timeout × 3 | offline |
| age > timeout | stale |
| fresh starting/draining | starting/draining |
| 其他 fresh phase | healthy |

age 在公开 snapshot 中不小于 0；clock regression 通过独立枚举表达，颜色不能成为唯一信息载体。

## Pursuit 生产接入

`PursuitLeaseSession` 在以下边界写入真实 heartbeat：

1. lease acquire 成功后写 running/sequence 1；若初始化写入失败，立即释放刚取得的 lease 并拒绝 admission；
2. 每次 lease keepalive 成功后递增 sequence 并写 running；写失败会让 session fail closed；
3. 精确 epoch 释放成功后写 stopped；若 stopped 写失败，lease 释放仍保持权威，旧 heartbeat 随 timeout 变 stale。

同 owner/epoch 的幂等重新 acquire 会先读取既有 sequence 后继续递增，不从 1 覆盖新 snapshot。Pursuit 的
heartbeat timeout 取 `min(lease_seconds, ceil(renew_interval × 2.5))`，下限 3 秒。

## 验收证据

- Harness DB 从 v11 幂等升级到 v12，旧表保留并新增 `harness_heartbeats`；
- heartbeat 可跨 Store reopen 读取，完全相同写入幂等；
- stale sequence、同 epoch 换 instance、旧 epoch、时间倒退被机械拒绝；
- 新 epoch takeover 可从新 instance/sequence 1 写入；
- healthy/stale/offline/starting/draining/stopped/failed/clock_regression 阈值有确定性测试；
- workspace 隔离与字段边界有错误路径测试；
- 真实 SQLite + Pursuit keepalive 证明 acquire、renew、release 对应 running/running/stopped；
- heartbeat admission 失败会释放刚取得的 lease，不遗留幽灵 owner；
- 只运行 heartbeat、HarnessStore、Pursuit lease/相关组合测试，不运行全量测试。

## 当前不足与下一切片

- 当前只接入 Pursuit execution domain；browser、background daemon、subagent 和 runtime service 尚未接入；
- 只有 latest snapshot，没有有界历史/丢包统计、jitter 或 crash-loop 事件；
- UI-13.1b 已把 active Worker contract 与对应 Harness heartbeat 组合为严格只读 Doctor 检查；UI-18 仍只展示
  当前 Pursuit recovery 的局部事实，尚无通用 Worker 列表；
- heartbeat 与 lease renew 是同一 Store 中的两个顺序事务，不是单 SQL 原子提交；
- offline 只是一种诊断状态，不会自动 takeover、kill 或 restart；这些属于 ARC-04.6/HAR-10.5。

UI-13.1b 已完成 typed heartbeat 到 Doctor 的第一个跨域只读投影，HAR-10.2c 已让默认 New UI Bridge 成为
真实 runtime producer。下一最小切片应根据依赖选择 browser/agent producer、runtime worker list/retention，
或让 UI-18 消费通用 Worker 健康；不要直接扩张成完整 Supervisor。
