# HAR-08.4o3e Sandbox Retry Receipt-Bound Resume

## 状态

已实现，2026-07-23。

本切片把 HAR-08.4o3d 只能读取的 `pending/recovery_required` dispatch 接到真实恢复执行链。新进程可以使用
既有 retry action、retry receipt 与 dispatch identity 获得一张新的工具调用权限回执，再由 Store 原子
claim 下一 generation ticket，继续原始 Request Manifest 与连续 H5a 前缀。

本切片不重新消费 cancel receipt、不创建第二个 retry intent、不改变业务 request authority，也不在 Bridge
启动时自动重放任务。

## 先修复的崩溃窗口

此前 `authorize_sandbox_admission_retry()` 只写 retry receipt，直到
`claim_sandbox_retry_dispatch()` 才创建 dispatch。进程若在两次调用之间退出，会留下 accepted receipt，
但 catalog 无法发现它，所谓 durable recovery 并不完整。

现在 accepted retry receipt 与 `pending` dispatch 在同一个 `BEGIN IMMEDIATE` 事务中提交：

1. cancel receipt、source ticket、Request Manifest 与一次性消费条件全部通过；
2. 写入 accepted `hsarr_` receipt；
3. 由 receipt 的不可变字段与 `created_at` 派生 `hsard_` dispatch identity；
4. 写入 `pending` dispatch，owner/ticket/epoch 均为空；
5. 同时 commit。

相同 action 的幂等重放会校验并恢复同一 receipt；若历史 v20/v21 数据只有 accepted receipt 而没有
dispatch，会以 receipt 原始时间补建同一个确定性 pending dispatch。已有 dispatch 必须与 receipt authority
完整一致，否则冲突关闭。`claim_sandbox_retry_dispatch()` 保留 legacy fallback，但也使用 receipt 原始时间派生
identity。

本改动只补齐 Store v21 的事务语义，不新增列或索引，因此 schema version 仍为 21。

## Resume Authority

新增 Agent Tool：

```text
harness_eval_sandbox_resume(
  retry_action_id,
  dispatch_id,
  retry_receipt_id,
  retry_receipt_sha256,
  run_id
)
```

新增共享 Slash：

```text
/harness eval sandbox resume <retry-action>
  --dispatch <dispatch-id>
  --receipt <retry-receipt-id>
  --sha256 <retry-receipt-sha256>
```

Slash 通过 `AgentEngine.execute_tool()` 进入同一 Tool/Service/Executor；Textual TUI 与 New UI 的共享 Slash
通道不各自读取 Store。New UI 的 typed Sandbox Eval parser 明确排除 `resume`，避免把它误识别为 check ID。

当前工具调用产生的新 `PermissionDecisionReceipt` 精确绑定：

- retry action；
- dispatch ID；
- retry receipt ID 与 SHA-256；
- 当前 Runtime run ID；
- `bash_run` delegation。

它只授权“执行这次 resume 调用”，不生成新的业务 execution authority。实际 Sandbox admission、Runtime
lease 与 Run Grant 仍使用原 accepted retry receipt 的 `execution_authority_key`；每次 crash recovery 只轮换
dispatch owner/epoch、ticket 与 batch-scoped Run Grant。

## 服务端验证链

`HarnessSandboxEvalExecutor.resume_retry()` 在 claim 前执行：

1. 严格校验 action、dispatch、receipt 与 SHA 格式；
2. 读取当前持久 Permission receipt；
3. 要求 tool 为 `harness_eval_sandbox_resume`，包含 `bash_run` delegation，且参数摘要完全一致；
4. 按 action 读取 accepted retry receipt，复验 receipt identity/digest；
5. 按 action 读取 dispatch，复验 dispatch ID；
6. 复验 dispatch 与 receipt 的 receipt SHA、Request Manifest SHA、execution authority；
7. 只按服务端 `eval_request_sha256` 读取原 Request Manifest；
8. 再进入公共 `execute()` 时重复校验 parent permission、receipt、dispatch 和 Profile；
9. 由 `claim_sandbox_retry_dispatch()` 在事务中判定当前 generation 是否可接管；
10. 从 Store-confirmed H5a 连续前缀继续执行。

客户端不能提交 checks、samples、batch、suite、workspace、revision、预算、owner、epoch、ticket 或 execution
authority。

## Fence 与状态语义

Resume 权限本身不根据 catalog 文本判断是否可接管。执行瞬间以 Store 为准：

| 当前事实 | 结果 |
|---|---|
| pending dispatch | 创建第一张 ticket 并 claim |
| claimed + expired ticket | reaper 确认 expired，递增 dispatch epoch，创建全新 ticket |
| claimed + live queued/active ticket | fail-closed，禁止并发 owner |
| ticket 已 completed/failed/cancelled、dispatch 未同步 | 先对账终态，再拒绝复活 |
| dispatch 已终态 | 拒绝 |
| receipt/dispatch/manifest 任一漂移 | claim 前拒绝 |
| H5a 非连续或超过 requested samples | 执行前拒绝 |
| H5a 已完整、dispatch 尚未终态 | claim 一张新 generation ticket 后直接完成 dispatch；不签发 Run Grant、不重跑 sample |

因此 catalog 的 `recovery_required` 只是发现事实；Permission receipt 是本次操作权限；Store ticket/dispatch
事务才是最终执行 fence。三者不能相互替代。

## 用户体验

`/harness eval sandbox retries` 现在显示 retry receipt ID/SHA。对 `pending` 与
`recovery_required` 项目，它生成完整、可复制的 resume 命令：

```text
/harness eval sandbox resume hsar_...
  --dispatch hsard_...
  --receipt hsarr_...
  --sha256 ...
```

输出明确说明：

- catalog 本身仍只读；
- resume 复用既有 receipt；
- 不创建新 action；
- 不重复消费 cancel receipt。

执行期间继续投影既有 `harness_sandbox_eval_progress`，New UI/TUI 都显示原 batch/check IDs 与真实
queued/admitted/recovering/executing/completed 状态。

## 验收证据

- accepted retry authority 同事务产生可跨 Store 读取的 pending dispatch；
- retry catalog 将该原子 pending dispatch 归类为 `pending`，不伪造 ticket/lease；
- 相同 action 重放保持同 receipt/dispatch identity；
- 真实 2/5 H5a 前缀、expired ticket 与新 Permission receipt 恢复到 5/5；
- retry receipt 对象恢复前后完全相同，没有第二次消费 cancel receipt；
- dispatch epoch 从 1 增至 2，ticket ID 轮换，旧 ticket 不复活；
- resume parent permission 精确绑定 dispatch/receipt/run；
- resume 的五个真实 `bash_run` 子回执全部挂到新 resume parent receipt；
- 真实 Git 工作区、真实 Harness SQLite、真实隔离 shell Worker 从 expired dispatch 执行到 completed；
- Tool 注册、权限规则与共享 Slash 使用同一 Service；
- New UI parser 将 `resume` 保留在共享 Slash channel；
- focused ruff、compile、Python/JavaScript 小模块测试通过。

## 自我审视与未完成

已确认：

- 不是用新 reason/actor 伪造首次 retry；
- 不是把 catalog cursor 当权限凭据；
- 不依赖旧进程内存、owner ID 或客户端重述业务参数；
- pending dispatch 已真正关闭 receipt commit 到首次 claim 之间的崩溃窗口；
- live、用户取消、真实失败和完成不会被 crash recovery 偷换语义；
- New UI 与 TUI 共享 Tool/Slash/进度权威。

HAR-08.4o3f 已实现 Bridge/TUI 启动时的 20 项有界 recovery snapshot，并在 New UI/TUI 建立只发现、
不自动 claim 的人工恢复队列。

仍未实现：

- resume 的 New UI 专用 typed action/button；当前可复制命令走共享 Slash；
- dispatch detail 页面与 retention 的保护集合、preview/prune receipt；
- 多客户端同一 recovery item 的可视化竞争提示；
- 跨主机 admission；
- Linux/Windows 真实隔离 Worker CI。

下一切片应横向比较 HAR-06 retention 与 dispatch detail，优先补只读详情和 retention
保护集合/preview；不得因为启动队列已可见就自动 claim 或删除记录。详见
`HAR-08-4o3f-sandbox-retry-startup-recovery-snapshot.md`。
