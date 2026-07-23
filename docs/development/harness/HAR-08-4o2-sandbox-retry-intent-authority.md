# HAR-08.4o2 Sandbox Retry Intent Authority

## 状态

已实现，2026-07-23。

本切片把一个 accepted Sandbox admission cancel receipt 一次性转换为新的 durable execution
authority。它解决 retry 的“谁有权重试、重试哪个不可变请求、是否已经消费”问题。

本切片不创建 admission ticket，也不启动 Worker。HAR-08.4o3 必须消费这里的 accepted receipt，
建立可恢复 dispatch、新 ticket 和真实 H5a 前缀恢复后，New UI/TUI 才能显示 retry 已启动。

## 为什么授权与 dispatch 分开

把“消费 cancel receipt、创建 ticket、启动异步执行”塞入一个前台调用会产生不可恢复窗口：

- 数据库提交前启动 Worker，崩溃会产生没有 durable authority 的执行；
- 数据库提交后启动 Worker，崩溃会留下已消费 receipt 却没有可恢复 dispatch；
- 只返回新 ticket，但没有原始 request，会形成空壳重试。

因此 HAR-08.4o2 只提交不可变 retry intent。后续 dispatch 必须以该 intent 为输入建立 durable
outbox/claim 语义，而不是在本事务中做 best-effort 启动。

## Store v20

新增 `harness_sandbox_admission_retry_attempts`：

| 字段 | 含义 |
|---|---|
| `action_id` | `hsar_<24 hex>`，幂等 action |
| `receipt_id` | `hsarr_<24 hex>`，由 receipt digest 派生 |
| `cancel_receipt_id/sha256` | 用户明确引用的 cancel authority |
| `source_ticket_id` | 被取消的精确 admission ticket |
| `source_authority_key` | 被取消 execution attempt 的 authority |
| `eval_request_sha256` | 由服务端解析的原始 Request Manifest |
| `execution_authority_key` | accepted 时生成的新 SHA-256；rejected 为空 |
| `decision/code` | 闭集裁决 |
| `actor_id/reason/created_at` | 审计事实 |
| `action_sha256/receipt_sha256` | 幂等与篡改检测 |

两个 partial unique index保证：

- 一个 accepted cancel receipt 最多产生一个 accepted retry authority；
- 一个 accepted execution authority 在 workspace 内唯一。

rejected 尝试也会持久化，但不消费 cancel receipt，因此用户修正 digest、时钟或其他输入后可以用新的
action 再试。

## 原子裁决

`HarnessStore.authorize_sandbox_admission_retry()` 在一个 `BEGIN IMMEDIATE` 中：

1. 校验 action、cancel receipt identity/digest、actor、reason、时钟和服务端 token；
2. 相同 action + 相同请求返回首次 receipt，不使用新的 token 或时间改写 authority；
3. 相同 action 被不同请求复用时冲突关闭；
4. 按 receipt ID 读取并完整复验 cancel receipt；
5. 只接受 `decision=accepted`；
6. 复验 source ticket 仍为相同 authority/epoch 的 `cancelled` 终态；
7. 从 source authority 解析原始 Request Manifest；
8. 检查 cancel receipt 尚未被另一个 accepted retry 消费；
9. 由服务端随机 token、cancel authority、request SHA、action 和时间派生全新 execution authority；
10. 写入 accepted/rejected 的防篡改 receipt。

调用方不能提交新的 checks、samples、batch、workspace、request SHA 或 execution authority。

## 链式 retry

第一次执行的 admission authority 等于原始 Request SHA。retry dispatch 以后将使用新的
`execution_authority_key`，因此“取消 retry 后再次 retry”不能直接拿 execution authority 查询 manifest。

Store 的解析顺序是：

1. 先按 source authority 直接查 Request Manifest；
2. 若没有，按 accepted retry receipt 的 execution authority 找到上一跳；
3. 从上一跳的 `eval_request_sha256` 重新读取并验证原始 manifest。

每一跳都产生新的 authority，不复活旧 ticket，也不把新 authority 当作新的业务 request。

## 稳定裁决码

接受：

- `sandbox_batch_retry_authorized`

拒绝：

- `sandbox_batch_retry_cancel_receipt_not_found`
- `sandbox_batch_retry_cancel_receipt_mismatch`
- `sandbox_batch_retry_cancel_not_accepted`
- `sandbox_batch_retry_clock_rollback`
- `sandbox_batch_retry_source_ticket_invalid`
- `sandbox_batch_retry_request_manifest_missing`
- `sandbox_batch_retry_cancel_receipt_consumed`

读取时 decision、code、action digest、receipt digest、identity 与 accepted/rejected 字段组合全部复验。

## 共享服务入口

`HarnessSandboxBatchAdmission.authorize_retry()` 是当前唯一上层入口：

- workspace 从 durable admission instance 获取；
- authority token 在服务端生成且不持久化；
- Store 只保存派生后的 execution authority；
- 非 durable admission 明确拒绝；
- Store/参数错误转换为稳定中文 `HarnessSandboxBatchError`。

Agent Tool、Slash、Bridge 和两套终端 UI 尚未接入，避免把 intent receipt 误呈现为已开始执行。

## 验收证据

- Store schema 从 v19 升级到 v20，完整表目录与历史迁移版本更新；
- accepted cancel + manifest 产生不同于 source 的 execution authority；
- 新 Store 实例和相同 action 重放恢复首次 receipt；
- 相同 action 的不同请求冲突关闭；
- 同 cancel receipt 的第二个 action rejected，且不产生 authority；
- 两个独立 Store facade 并发消费时精确一个 accepted；
- missing/mismatched/rejected cancel、时钟回退、缺失 manifest 全部 rejected；
- 取消 retry authority 后可沿上一跳解析回同一原始 request，再生成第三个不同 authority；
- admission facade 使用服务端 token；
- 篡改 action digest 后首次读取立即失败；
- 聚焦 Store/Request/Admission 测试、ruff、compile 与 diff check 通过。

## 自我审视与限制

已确认：

- 这不是只生成随机 ID；accepted authority 必须通过 cancel、ticket、manifest 和一次性消费事务；
- rejected 尝试不静默吞掉用户后续修正机会；
- 并发消费由 SQLite 写事务和 partial unique index共同保护；
- retry chain 保持业务 request 不变，同时轮换 execution authority。

仍未实现：

- retry intent 的 durable dispatch/outbox 状态机；
- 新 admission ticket 与 retry receipt 的绑定；
- 新 permission receipt、Runtime lease、Run Grant 和真实 H5a 恢复；
- dispatch 崩溃后的 claim/lease/recovery；
- Tool、Slash、Bridge、New UI 与 TUI surface。

## 下一切片

HAR-08.4o3 应实现 retry dispatch authority。它必须消费 accepted `hsarr_` receipt，创建全新 ticket，
以 receipt 中的 execution authority 进入 durable admission，并从 `eval_request_sha256` 恢复原请求。
dispatch 必须可在进程崩溃后重新 claim，不能依赖一次前台协程完成启动。
