# ARC-04.3c Run-Delegated Shell Admission

## 目标

把 UI-12.3b3/3b4 的有界 Run Grant 接入生产 `ShellWorkerAdmissionComposer`，让长周期 Harness/Evolution
编排可以重复执行精确 Sandbox check，同时保持每个命令的 child receipt、ExecutionGrant、ToolJob、Worker
incarnation 与 Tool Run Lease 都是短期且独立复验的 authority。

## 生产链路

Composition Root 现在创建并共享：

- `.naumi/run-delegation-grants.db` 对应的 `RunDelegationGrantStore`；
- 绑定当前 workspace、Permission Store 与 Harness Store 的 `RunDelegationGrantAuthority`；
- 同一 authority 注入 Engine 的 ExecutionGrant authority 与 Shell admission composer。

`ShellWorkerAdmissionComposer.compose()` 保留一次性 `parent_receipt_id` 路径，并新增显式、任务局部的
`run_grant_id`。它不读取全局“当前 Run Grant”，也不把 grant 放进永久 Session 状态。

当提供 `run_grant_id` 时，Composer 按以下顺序失败关闭：

1. 在创建 Worker 或 Tool Lease 前验证 Run Grant、父 digest、session/run 与 `bash_run` scope；
2. 将 Run Grant digest 纳入 admission identity；
3. 创建新的 Tool Worker incarnation 与 snapshot workspace Tool Lease；
4. 通过 Permission Store schema v4 签发精确参数、最多 120 秒的 child receipt；
5. 通过同一 Run Grant authority 签发 delegated ExecutionGrant；
6. ToolJob admission 再次复验 ExecutionGrant；
7. dispatch 前再次检查 child expiry 与 Run Grant 撤销/lease fence；
8. `finally`/显式 release 只清理本次 Tool Lease 与 Worker，不替 outer cohort 撤销 Run Grant。

## 验收证据

- 缺失或无效 Run Grant 在 Worker registration 前被拒绝，无残留 Worker history；
- 父回执签发超过 300 秒后，真实 macOS sandbox worker 仍通过有效 Run Grant 执行命令并返回 passed；
- 同一 Run Grant 创建第二个独立 Worker/ToolJob 后，撤销 grant 会在 dispatch 阶段返回
  `execution_grant_invalid`，Shell payload 未发送；
- 两个 Worker 使用不同 incarnation/epoch，release 后 Tool Lease 与 registration 均被清理；
- RuntimePaths/Resources/Engine 全部引用同一个 typed Store/Authority，构造阶段不提前创建数据库。

## 尚未完成

本切片完成了 EVO-03 interventional RED executor 的最后一个权限/执行前置，但没有冒充 cohort 已实现。
下一切片应新建 executor：重验 Validation Contract/Plan/Binding/Request，取得 outer Runtime Run Lease 与
Run Grant，为每个 sample/check 调用既有 `HarnessSandboxCheckRunner`，将结果写入既有 H5a Store，并在取消、
异常和终态撤销 grant、释放 outer lease。
