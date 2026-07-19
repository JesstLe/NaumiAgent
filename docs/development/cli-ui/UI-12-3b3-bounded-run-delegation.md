# UI-12.3b3 有界 Run Delegation Grant

## 目标

为耗时可能超过一次权限回执 freshness 窗口的 Harness/Evolution 编排提供独立、可撤销且持续复验的授权
authority。它解决的是“一个已获准的外层运行如何在最多 3600 秒内分批派生短期子调用”，不是延长所有工具
回执或赋予长期万能权限。

## 已实现合同

`RunDelegationGrantAuthority` 仅能在父权限回执签发后 300 秒内创建 grant，并机械绑定：

- 父回执 ID 与 SHA-256；
- 精确 `session_id`、`run_id` 与 workspace digest；
- Harness Run Lease 的 kind、owner 与 epoch；
- 父回执已经声明的下游 Tool 白名单子集；
- 最长 3600 秒的硬截止时间，且不得晚于签发时 Run Lease 的截止时间；
- 唯一 idempotency key、请求摘要与防篡改 grant 摘要。

Store 使用独立、版本化 SQLite authority。合同不可变，撤销事实可持久化；重启后仍可重新读取。验证不会因
父回执超过 300 秒而失效，而会在每次使用前重新检查 grant 状态、硬截止时间、父回执 digest/scope、
workspace 以及当前 Run Lease owner/epoch/state/expiry。释放租约、epoch takeover、撤销、超时或持久化篡改
均失败关闭。

## 安全边界

- 未修改 `PermissionDecisionReceiptStore.issue_delegated()` 的 300 秒父 freshness 和 120 秒子回执上限；
- 未修改 `ExecutionGrantAuthority` 的 300 秒回执 freshness 和 300 秒执行 grant 上限；
- 不接受 delegated 子回执或 session grant 创建运行委托；
- 不保存命令参数、API Key 或其他 secret；
- grant 本身不能执行 Tool，也不能递归扩权；它只是后续短期子授权的可复核父 authority；
- 初始 Run Lease 后续即使续租，也不会自动延长 grant 的既定硬截止时间。

## 验收证据

- grant 在父回执已超过 300 秒后仍可通过复验，但不超过自身/租约截止时间；
- Store 重开后合同、父摘要、lease fence 与 scope 保持一致；
- scope 扩张、过期父回执、已释放 lease、撤销 grant 和篡改合同均拒绝；
- expiry 被较短的 Run Lease 截止时间机械截断；
- 相关 Ruff 与 5 个定向单元测试通过，未运行全量测试。

## 尚未完成

本切片只交付 run authority，不把它冒充已完成的 cohort executor。下一切片需要：

1. 为短期 delegated permission receipt 增加精确 `run_grant_id` 绑定；
2. 签发子回执和 delegated ExecutionGrant 前后都重新读取本 Store；
3. Shell admission composer 接受任务局部 run grant context；
4. 然后由 EVO-03 interventional RED executor 获取一个 Runtime Run Lease，顺序执行既有
   `HarnessSandboxCheckRunner`，并在取消/终态释放 lease、撤销 grant。
