# EVO-05.3b2a Target-advance Rebase Authority

## 1. 目标

关闭 Revalidation Request 的 authority 死锁：Request 在审批时绑定 exact target，但 target 一旦线性前进，旧实现会把
它一律标记为不可执行，因此文档承诺的隔离 rebase 永远没有合法输入。

本切片允许且仅允许以下情况进入后续三方 rebase executor：

1. 原始 Decision 确实为 `approved`；
2. 只有 target movement 使当前 Decision 变 stale；
3. Requirement 未过期，Input 与 Reflection 仍 active；
4. 所有角色原本 approved，当前 Response、Principal、key、role binding 与密码学签名除 target 外仍 current；
5. 当前 target 是 Request target 的线性后代。

它不执行 rebase 或文件写入，只修复“谁有资格启动隔离 rebase”的机械 authority。

## 2. Decision target-only stale

`EvolutionPromotionApprovalDecisionView` 新增 `target_only_stale`。它不能仅凭 `status=stale` 推断，而是重读当前完整
authority 后逐项证明：

- 原始 Receipt 是 approved；
- 当前 package/input/reflection 仍有效，Requirement 未过期；
- 唯一 stale technical gate 是 `TARGET_CURRENT`；
- 原始 role decisions 全部 approved；
- signed role 即使因 target 改变显示 `signature_stale`，其 Requirement、Response、Principal active、key generation、
  role binding 均仍有效；
- 当前 signature 数量仍等于原批准要求。

Principal key 轮换、撤销、角色变化、Requirement 到期、Reflection 撤销或其他 gate 变化均不能获得该投影。

`current_rebase_revalidation_eligible` 现在表示“exact approval 仍 current，或只有 target 线性重放前置发生变化”，而非
Promotion 资格。该字段仍不授权 merge、push 或 publish。

## 3. Request 当前 target 关系

`EvolutionRevalidationRequestService.inspect()` 以 Request 原 target head 为 baseline，重新探测当前 target branch，形成：

- `same`：exact replay 可执行；
- `advanced`：只有 `target_only_stale` 且非 target authority 均有效时，隔离 rebase 可执行；
- `diverged`：必须人工 reconciliation，不可自动执行；
- `unavailable`：不可执行。

View 同时公开 `current_target_head`、`current_target_relation` 和 `decision_rebase_eligible`。`current_status` 在 target
前进时仍保持 `stale`，避免 UI 把旧审批误显示为 current；但 `execution_eligible=true` 精确表示只允许进入隔离
rebase/revalidation。Promotion 仍需后续新验证证据和新决策。

Package current 的动态判断拆分为两层：Input/Reflection 与 immutable Package digest 仍 current，不再因为 target 单独
移动就丢失 package authority；target current 由独立关系探测表达。

## 4. 验收证据

- approved Request 的 target 线性前进后：status stale、relation advanced、rebase eligible true；
- Principal key 轮换后：relation same 也必须 ineligible；
- target 指向无共同祖先的 commit：relation diverged、execution eligible false；
- exact target 仍保持 ready/execution eligible；
- 既有 exact Replay Executor 看到 advanced authority 时明确返回“rebase executor 尚未实现”，不会退化为文件覆盖；
- 仅运行 Approval Decision、Request、Replay 相邻模块测试，未运行全量测试。

## 5. 剩余工作

EVO-05.3b2b 需要消费本 authority，在 current target 的 detached worktree 中执行真实三方 merge，持久化 success/conflict
artifact，并加入跨进程 claim、epoch fencing、崩溃恢复和残留 worktree 清理。完成前，`execution_eligible=true` 仅表示
合法输入已形成，不表示 rebase 已发生。

