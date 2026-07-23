# HAR-08.4o3a Sandbox Retry Durable Dispatch

## 状态

已实现，2026-07-23。

本切片把 HAR-08.4o2 的 accepted retry intent 转换为可崩溃恢复的 dispatch claim，并在同一
SQLite 事务创建全新的 admission ticket。它关闭“intent 已提交，但前台协程尚未创建 ticket”以及
“ticket 已创建，但 dispatch 尚未记录”的双向崩溃窗口。

本切片不调用 Sandbox Eval Executor，也不开放 Tool、Slash 或 UI。HAR-08.4o3b 必须在本 dispatch
上下文中恢复 Request Manifest、取得新权限/Run Grant 并真实继续 H5a。

## Store v21

新增 `harness_sandbox_retry_dispatches`：

| 字段 | 约束 |
|---|---|
| `dispatch_id` | `hsard_<24 hex>`，由 immutable dispatch digest 派生 |
| `retry_action_id` | workspace 内唯一，一个 intent 只有一个当前 dispatch |
| `retry_receipt_id/sha256` | 精确绑定 HAR-08.4o2 accepted receipt |
| `eval_request_sha256` | 原始业务 Request Manifest |
| `execution_authority_key` | 本次 retry 的新 execution authority |
| `state` | `pending/claimed/completed/failed/cancelled` |
| `owner_id/epoch` | dispatch generation fence |
| `ticket_id/ticket_epoch` | 当前 claim 创建的精确新 ticket |
| `created_at/updated_at/terminal_code` | 生命周期事实 |
| `request_sha256` | immutable dispatch 字段摘要 |

`retry_receipt_id` 唯一；非空 `ticket_id` 通过 partial unique index 唯一。读取时重新校验
dispatch digest、identity、时间单调性和每个 state 的字段组合。

## 原子 claim

`HarnessStore.claim_sandbox_retry_dispatch()` 在一个 `BEGIN IMMEDIATE` 中：

1. 回收 workspace 中过期 admission tickets；
2. 读取 retry action，并复验 accepted receipt identity/digest；
3. 读取、复验原始 Request Manifest；
4. 首次 claim 创建稳定 dispatch；
5. 已 terminal dispatch 拒绝；
6. 已 claimed 且 ticket 仍 queued/active 时拒绝并发 owner；
7. 当前 ticket 为 completed/failed/cancelled 时把 dispatch 对账到同终态并拒绝复活；
8. 只有当前 ticket 为 `expired` 时允许 crash recovery；
9. 拒绝 source ticket、上一 generation ticket 或任意已占用 ticket ID；
10. 复用 workspace admission policy 与 active/queued 容量；
11. 原子插入新 ticket，并把 dispatch 更新为新 owner、递增 epoch 和该 ticket。

ticket 的 lane、requested samples 和 authority 全部来自服务端 manifest/retry receipt，调用方不能
重新声明。

## 恢复与取消语义

- 进程崩溃：ticket lease 到期后由既有 reaper 标记 `expired`；下一 owner 可以递增 dispatch epoch，
  创建另一个全新 ticket。
- live owner：第二个进程不能接管或并发创建 ticket。
- 用户取消：ticket 进入 `cancelled`，dispatch 同步进入 `cancelled`；同 retry intent 不得自动恢复。
- 真实执行失败：ticket/dispatch 进入 `failed`；不得用 crash recovery 偷换成自动 retry。
- 正常完成：ticket/dispatch 都进入 `completed`。
- ticket 已终态但进程在同步 dispatch 前崩溃：下一次 claim 会先对账终态，然后拒绝复活。

旧 owner 的 dispatch epoch、ticket ID 或 ticket epoch 任一不匹配，终态提交都会失败。

## Admission facade

`HarnessSandboxBatchAdmission.admit_retry()` 复用既有 durable admission 生命周期：

- 服务端 token 生成新 ticket/owner；
- `claim_sandbox_retry_dispatch()` 替代普通 enqueue；
- queued polling、active renewal、本进程 task cancellation 与 typed transition 保持不变；
- context 正常退出、异常、用户取消时先结束 ticket，再用同 owner/epoch/ticket fence 结束 dispatch；
- ticket 为 `expired` 时保留 claimed dispatch，供 crash recovery 创建下一 generation。

非 durable admission 明确拒绝 retry dispatch。

## 验收证据

- Store schema 从 v20 升级到 v21，完整表目录和历史迁移版本更新；
- accepted intent 原子创建 dispatch 与不同于 source 的新 ticket；
- ticket authority 等于 retry execution authority，samples 来自 Request Manifest；
- live ticket 阻止第二 owner；
- ticket lease 过期后新 Store instance 创建另一个 ticket，dispatch epoch 递增；
- 旧 owner 无法提交新 generation 的终态；
- completed ticket/dispatch 可跨 Store instance 恢复，且不能再 claim；
- admission facade 正常退出后两者均 completed；
- 用户取消真实 owner task 后两者均 cancelled，不能按 crash 恢复；
- 非 durable facade 拒绝；
- 篡改 dispatch digest 后读取立即失败；
- 聚焦 Store/Request/Admission 测试、ruff、compile 与 diff check 通过。

## 自我审视与限制

已确认：

- dispatch 与 ticket 在同一事务产生，不存在只成功一半的持久状态；
- crash recovery 只识别 `expired`，不会复活用户取消或真实失败；
- 每次恢复都创建新 ticket，而不是提高旧 ticket epoch；
- retry intent、manifest、execution authority、dispatch 和 ticket 形成可机械复核的链。

仍未实现：

- 从 dispatch 恢复 Request Manifest 后的真实 Sandbox Eval Executor 调用；
- retry 专属 permission receipt 与父权限参数；
- Runtime lease、Run Grant、H5a 连续前缀恢复的端到端组合；
- dispatch catalog/retention；
- Agent Tool、Slash、Bridge、New UI 与 TUI。

## 下一切片

HAR-08.4o3b 应让 `HarnessSandboxEvalExecutor` 接受 retry dispatch context：业务 kernel 继续使用原始
request SHA，admission/coordinator 使用新的 execution authority，并以新 permission receipt 和 Run Grant
恢复未完成 H5a。只有该真实链路通过后，才能进入 UI surface。
