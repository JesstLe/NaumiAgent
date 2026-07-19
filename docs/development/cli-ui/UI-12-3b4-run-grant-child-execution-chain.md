# UI-12.3b4 Run Grant 子回执与 ExecutionGrant 闭环

## 目标

让 UI-12.3b3 的有界 Run Delegation Grant 真正进入现有 Permission Receipt → ExecutionGrant authority 链，
同时保持每个下游命令仍是精确参数、短时、不可递归的授权。长运行不得通过复制父回执、隐式刷新用户决定
或延长全局 TTL 获得执行权。

## 已实现

### Permission receipt schema v4

- delegated child 可选绑定 `run_delegation_grant_id` 与 grant SHA-256；两者必须同时存在或同时为空；
- `issue_run_delegated()` 在签发每个 child 前重新调用 Run Grant authority，检查撤销、截止时间、父链、
  workspace、Run Lease owner/epoch 和 Tool scope；
- child 继续绑定精确 arguments digest，TTL 上限保持 120 秒，并取 child TTL 与 Run Grant expiry 的较早值；
- v1/v2/v3 receipt 摘要保持只读兼容，Store 只提升数据库版本，不改写历史 JSON。

### ExecutionGrant 双重复验

- 签发 run-delegated ExecutionGrant 时必须显式提供同一 workspace 的 Run Grant authority；
- authority 重新读取 child、parent 与 Run Grant，复核 grant digest、session/run 和 Tool scope；
- delegated ExecutionGrant expiry 现在机械截断到 child receipt expiry，不再可能比子授权存活更久；
- dispatch 前 `validate()` 再次检查 child expiry 与 Run Grant 状态；用户撤销 Run Grant 后，已经签发但尚未
  dispatch 的 ExecutionGrant 立即返回 `authorization_invalid`。

## 验收证据

- 父回执超过 300 秒后，可由仍有效的 Run Grant 签发精确 120 秒 child；
- child 持久化并重开后保留 Run Grant id/digest；撤销后新 child 签发失败；
- run-delegated ExecutionGrant 可正常签发，Run Grant 撤销后 dispatch 复验失败；
- 普通 delegated ExecutionGrant 的 expiry 被 30 秒 child 截断，过期后同时报告 grant expiry 与
  authorization invalid；
- v1/v2/v3 migration、直接授权和原有 delegated 路径仍通过定向回归。

## 尚未完成

ARC-04.3c 已让 Shell admission composer 接受任务局部 `run_grant_id` + authority：每次 check 通过 schema
v4 child 再签发 ExecutionGrant，并在 compose 与 dispatch 之间持续复验。下一最小切片可以进入 EVO-03
interventional RED cohort，顺序消费既有 Sandbox Runner。
