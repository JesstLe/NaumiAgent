# HAR-08.4o3b Sandbox Retry Execution

## 状态

已实现，2026-07-23。

本切片把 HAR-08.4o3a 的 durable dispatch 接到真实 Sandbox Eval 执行链。用户取消一个正在运行的
Sandbox Eval 后，可以用 accepted cancel receipt 发起一次显式 retry；服务端恢复原始 Request Manifest，
创建新的 execution authority、admission ticket、Runtime lease 和 Run Grant，并从已持久化的连续 H5a
前缀继续执行。

本切片开放 Agent Tool 与共享 Slash 命令。HAR-08.4o3c 已在 New UI 增加可点击 retry action，Textual TUI
继续使用共享 Slash；两端在 retry 运行期间复用既有 typed Sandbox progress，不新增第二套进度协议。

## 权威分层

retry 不得把“要评测什么”和“本次谁有权继续执行”混为一个字段：

| 权威 | 来源 | 用途 |
|---|---|---|
| business request authority | 原始 `HarnessSandboxEvalRequest.request_sha256` | Kernel、源码身份、H5a 与最终 batch receipt |
| execution authority | accepted retry receipt 的 `execution_authority_key` | 新 admission ticket、checkpoint、Run Grant idempotency |
| parent permission | 当前 `harness_eval_sandbox_retry` 权限回执 | 精确绑定 action、cancel receipt、reason、run |
| dispatch fence | retry action + receipt + owner/epoch/ticket | 崩溃恢复与终态提交 |

因此，retry 后形成的新 H5a sample 仍属于原 batch/suite/request，不能伪装成第二个业务评测；但旧 ticket、
旧 Run Grant 和旧 execution authority 均不会复活。

## Service 与 Executor

`HarnessService.retry_sandbox()` 和 `HarnessSandboxEvalExecutor.retry()` 执行以下闭环：

1. 在消费 cancel receipt 前确认当前 Harness Profile 仍受信任；
2. 读取当前持久 permission receipt；
3. 要求 tool 为 `harness_eval_sandbox_retry`，包含 `bash_run` delegation；
4. 机械校验参数摘要精确等于：
   - `retry_action_id`
   - `cancel_receipt_id`
   - `cancel_receipt_sha256`
   - `reason`
   - permission receipt 自身的 `run_id`
5. 调用 HAR-08.4o2 authority，一次性消费 accepted cancel receipt；
6. 只根据 retry receipt 的 `eval_request_sha256` 读取服务端 Request Manifest；
7. 再次校验 retry receipt、Request Manifest 与 execution authority 的完整链；
8. 在每个 sample 前后继续复验当前 Profile；
9. 让 Kernel 继续使用原 request SHA；
10. 让 Coordinator 使用新 execution authority，并通过 `admit_retry()` claim HAR-08.4o3a dispatch；
11. 以新父权限回执签发新的 batch-scoped Run Grant；
12. 从 Store 中的连续 H5a prefix 继续到原 `requested_samples`；
13. admission ticket 与 dispatch 同步进入 completed/failed/cancelled。

客户端不能重新声明 workspace、batch、suite、checks、samples、revision 或预算。所有业务参数只从原
Request Manifest 恢复。

## Tool 与 Slash

新增 Agent Tool：

```text
harness_eval_sandbox_retry(
  retry_action_id,
  cancel_receipt_id,
  cancel_receipt_sha256,
  reason,
  run_id
)
```

新增共享 Slash：

```text
/harness eval sandbox retry <cancel-receipt> --sha256 <digest> [--reason <原因>]
```

Slash 只生成一次新的 action ID 和当前 session run ID，然后通过 `AgentEngine.execute_tool()` 调用同一 Tool；
不直接调用 Store 或 Executor。原 cancel 命令现在同时显示 receipt ID 与 SHA-256，用户不需要进入数据库
寻找 retry 所需摘要。

Tool 通过 retry action 读取服务端 manifest，把既有
`harness_sandbox_eval_progress` 投影为原 batch/check IDs。New UI 与 Textual TUI 因而继续看到真实
queued/admitted/recovering/executing/completed 状态；不会显示伪造的第二个 batch。

## 失败关闭与并发语义

- permission 参数不匹配：在 retry intent 产生前拒绝，不消费 cancel receipt；
- cancel receipt 不存在、摘要不符、未 accepted 或已消费：写入 HAR-08.4o2 rejected audit，不执行；
- Request Manifest 缺失或损坏：不接受 client fallback；
- live dispatch owner：第二调用被 fence；
- ticket lease expired：HAR-08.4o3a 创建新 generation ticket 后继续；
- 用户取消 retry：新 ticket/dispatch 进入 cancelled，不能当作 crash 自动恢复；
- sample 真实失败：新 ticket/dispatch 进入 failed，不能无声明重试；
- Profile 漂移：执行前或 sample 边界失败关闭；
- 原 H5a 已完整：返回同一完成 receipt，不创建虚假的新 sample；已消费 retry intent 保留为审计事实。

## 验收证据

聚焦自动化覆盖：

- 真实 durable admission 在 2/5 H5a 时取消 owner task；
- source ticket 保持 cancelled；
- accepted retry 绑定原 Request Manifest；
- retry ticket 与 source ticket 不同；
- retry execution authority 与原 request authority 不同；
- Kernel 的所有 sample（取消前与恢复后）均继续绑定原 request SHA；
- retry checkpoint 绑定新 execution authority；
- 恢复后 H5a 为连续 `0..4`；
- 最终 receipt 包含两个不同 Run Grant 摘要；
- retry dispatch 与新 ticket 同步 completed；
- permission reason 漂移在消费 intent 前被拒绝；
- Engine 注册 Tool，权限表在 moderate/bypass 下保持既有策略；
- Slash 使用真实 Git 工作区、真实隔离 shell Worker、真实权限/Run Grant/SQLite，在 2/5 取消后恢复至 5/5；
- 五个真实 `bash_run` 子回执中，后三个精确挂到 retry 父回执。

## 自我审视与限制

已确认：

- retry 不是重新提交原参数，而是恢复服务端不可变 request；
- business evidence 与 execution fencing 已明确分层；
- 新权限、新 ticket、新 lease、新 Run Grant 均真实产生；
- Tool 与 Slash 共用唯一 Service/Executor；
- 真实 shell 场景证明取消后不是从 0 重跑。

仍未实现：

- HAR-08.4o3d 已补齐 retry dispatch catalog；详情与 retention 仍未完成；
- 跨主机 admission；
- Linux/Windows 的真实隔离 Worker CI 证据。

HAR-08.4o3c 已增加共享 retry action protocol 和 New UI/TUI 双端投影，且没有复制本切片的授权或执行逻辑。
详见 `HAR-08-4o3c-sandbox-retry-ui-action.md`。
