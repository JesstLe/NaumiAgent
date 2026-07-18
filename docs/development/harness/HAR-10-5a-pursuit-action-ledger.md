# HAR-10.5a Pursuit 持久行动账本

## 目标

HAR-10.4b 能从安全 checkpoint 继续 planner loop，但 `action_inflight` 只能判定“外部副作用不明确”，无法回答
行动是否已经派发、后台 task ID 是什么、结果是否已回收。HAR-10.5a 为 Pursuit 的 shell/background 路径建立
权威行动生命周期，让重试与重启至少能区分 `prepared`、`dispatched`、`waiting` 和确定终态，不再把内存中的
tool call ID 当作恢复依据。

## 为什么不复用现有记录

- Harness `EvidenceCollector` 的 tool start 尚在内存，持久层只保存 tool end 的有界摘要，不能证明派发边界；
- ChatRun step 可按 sequence 更新，面向 UI 回执，不是不可变副作用身份；
- BackgroundTaskStore 能保存进程和输出，但调用方不能传入 idempotency key，Pursuit 也没有从计划行动到 task ID
  的权威映射；
- checkpoint 只保存当前计划和等待项，不能承担行动事件历史。

因此本切片继续使用已有 `pursuit.db`，不创建第二个调度循环，也不复制后台运行器。

## 数据契约

`PursuitActionRecord` 是严格、有界、禁止额外字段的 schema。稳定 `action_key` 由以下字段的 canonical identity
计算：

- `run_id`；
- 当前 iteration；
- planner `action_id`；
- 实际工具名；
- canonical arguments SHA-256。

同一个 iteration 的相同行动重试得到相同 key；下一 iteration 即使 planner 再次使用 `a1` 也得到不同 key。
数据库不保存原始 command/arguments，只保存摘要、字节数、digest、脱敏且有界的结果摘要和结果 digest。

## 单调状态机

允许的转换只有：

```text
prepared -> dispatched -> completed | failed
                       -> waiting -> completed | failed
```

- `prepared` 必须在任何外部调用前提交；
- `dispatched` 必须在调用 executor/background tool 前提交；
- `waiting` 绑定后台返回的真实 `bg_*` task ID；
- terminal 写入真实工具结果或后台状态/输出摘要；
- 相同写入幂等返回；状态倒退、同 key 异内容、不同终态回执均 fail closed。

`pursuit_actions` 保存经过认证的最新快照，`pursuit_action_events` 只追加事件。事件 sequence 连续，且每个事件
保存前一 payload digest；读取时同时校验 canonical payload SHA-256、哈希链、事件元数据和最新快照。SQLite
`BEGIN IMMEDIATE` 串行化 compare/write，同一行动的并发终态只有一个能成功。

## 生产接入

### 同步 bash

1. 模型命令先经过 HAR-10.8a 定向范围策略；
2. 在当前 Pursuit lease 下准备 action identity；
3. 写入 `dispatched` 后才调用 `bash_run`；
4. executor 明确返回后，在再次确认 lease 所有权后写入 terminal；
5. 相同行动已经 terminal 时复用持久回执，已经 dispatched 且无结果时提示 reconcile 并拒绝重跑。

### 后台命令

1. 以最终 `background_run` 参数建立独立 action identity；
2. 派发前写 `dispatched`；
3. 后台工具成功返回真实 task ID 后写 `waiting`；
4. `_collect_background_results()` 通过 `run_id + task_id` 找到同一行动，读取真实 status/output 后写 terminal；
5. 已 waiting 的重复调用返回原 task ID，不产生第二个进程。

旧版 Pursuit wait 没有 action ledger 时仍按原路径回收，保证已有数据可读；它不会被伪造为新账本记录。

## 验收证据

- SQLite 重开后可恢复完整 `prepared → dispatched → waiting → completed` 事件链；
- 相同 prepare/transition 幂等，同 key 异输入拒绝；
- 状态倒退和冲突终态拒绝，并发 writer 只有一个获胜；
- event/snapshot 篡改和哈希链断裂 fail closed；
- 数据库不出现原始 command、API key、token 或未脱敏结果；
- 相同 planner action ID 在不同 iteration 产生不同 key；
- injected executor 观察到的状态已经是 `dispatched`，重复调用次数保持 1；
- mock 后台与真实 Python 子进程都能从 waiting 回收到同一 terminal action；
- Pursuit、checkpoint、background 相关 158 个定向测试通过，不运行全量测试。

## 当前不足与下一切片

HAR-10.5a 交付“能证明已走到哪个派发边界”的权威账本，但尚未声称完整 exactly-once：

- `dispatched` 提交与外部 executor 真正接收之间不能跨存储原子提交；在这个窗口崩溃会诚实保持 ambiguous，
  不会自动重跑；
- BackgroundRunner 还不接受调用方 `dispatch_token`。进程已启动但 task ID 尚未回写时，Pursuit 只能要求
  reconcile，不能机械去重；
- 当前只接入 bash/background；file、browser、subagent、外部 API 需按各自可观测性逐域接入；
- HAR-10.4b 的 `reconcile_required` 还不会根据账本和外部状态自动解除；
- SHA-256 链用于发现存储损坏/非一致修改，不是带密钥的防恶意数据库管理员签名。

HAR-10.5b 已让 BackgroundRunner 接受并持久化 caller idempotency key，并允许 `dispatched` background
action 通过相同 key 安全重试。下一切片 HAR-10.5c 负责类型化外部状态核对与 checkpoint reconcile；随后
才扩展 browser/agent/API。
