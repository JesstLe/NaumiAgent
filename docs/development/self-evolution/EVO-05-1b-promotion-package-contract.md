# EVO-05.1b Promotion Package Contract

## 目标与边界

本切片把仍 eligible 的 EVO-05.1a Input 冻结成完整、可签名、绑定 exact local target branch 的 Promotion
Package，为 EVO-05.2 审批策略提供唯一输入。它只读取 Git identity，不执行 checkout、fetch、rebase、commit、
merge、push、publish、migration 或 rollback，也不收集签名或作出审批决定。

## Authority 链

`active Promotion Input → active Reflection → exact target branch snapshot → Promotion Package`

Executor 只接收 Promotion Input ID 和可选 target branch（默认 `main`）。Input 与 Reflection 从共享 SQLite
authority 库重读；Target Probe 使用无 shell、禁用 prompt/optional lock、5 秒超时的只读 Git 命令解析 repository
root、`refs/heads/<branch>`、commit、tree 以及 baseline relation。branch 名同时经过本地机械校验与
`git check-ref-format --branch`。

Store 在 `BEGIN IMMEDIATE` 内重读 exact Input 与 Reflection revocation，随后以
`(promotion_input_id, target_branch, target_head)` 单飞。目标分支每次移动都产生新的 successor Package，旧 Package
保留审计但动态失去 review eligibility。Store 落库前还会独立重跑 target probe，拒绝调用方伪造或捕获后已变化的
snapshot；同步 Git 子进程通过 worker thread 执行，不阻塞 async runtime event loop。

## Package 内容

### Target Snapshot

- canonical repository root、target branch/ref、head commit、tree；
- Input baseline commit 与 `same/advanced/diverged` 关系；
- 非 same 时机械设置 `rebase_required=true`、`revalidation_required=true`；
- 固定 `git_write_executed=false`。

### Approval Input

本切片只冻结审批事实，不实现 EVO-05.2 policy：risk、required platforms、changed files/lines、目标分支是否受保护、
migration/data-backup 要求，以及 authorization、CI/release、dependency、persistence、security 等受保护路径信号。
Artifact 固定 `approval_policy_evaluated=false` 与 `approval_decided=false`。

### Signature Envelope

域分隔符为 `naumi.evolution.promotion-package.review.v1`，signable digest 覆盖 Input、Target、Patch、Baseline、
Migration、Rollback 与 Approval Input 的 exact digest。它不选择签名算法或 signer，不保存 secret，固定
`signature_policy_evaluated=false`、`signature_collected=false`。

## 动态失效

- Input/Reflection 撤销或损坏：Package 保留，但 `package_review_eligible=false`；
- target branch 不存在或无法读取：`target_available=false`；
- target head/tree 与 Package 不同：`target_current=false`；
- successor 只能从最新 target snapshot 创建，不能覆盖旧 Package。

这些状态只控制 Package review，不代表 EVO-05.2 approval eligibility，更不代表 promotion readiness。

## 双通道与权限

- 用户：`/evolution promotion-package <promotion-input-id> [target-branch]`；
- Agent Tool：`evolution_promotion_package`；
- 两者共用 `EvolutionPromotionPackageExecutor`；New UI 透传共享 Slash channel，TUI 复用同一 router；
- permissive/moderate/strict 为 `MEDIUM`、无需逐次确认、每会话 50 次；lockdown 阻断；bypass 直接通过；
- bypass 不能绕过 Input/Reflection、target identity、digest 或 Store 冲突。

## 验收证据

- 真实临时 Git repo + SQLite authority：8 路并发生成同一个 Package；
- `main` 前进后旧 Package 动态 stale，新 Package 绑定新 head 并标记 rebase/revalidation；
- Reflection append-only 撤销后 Package 立即失去 review eligibility；
- 重算顶层 digest 也不能伪造 signature、approval 或 Git execution；恶意 branch 名被拒绝；
- Store 索引篡改被检测；公共 lazy export、Engine composition、Tool/Slash、权限和 New UI 透传有聚焦测试；
- Ruff、py_compile 与小模块测试通过，不运行全量测试。

## 后续依赖

EVO-05.2a 已从 still-current Package 冻结角色、签名门、protected-scope 人工门和 expiry。下一最小依赖是
EVO-05.2b Approval Request Authority 也已完成；下一步是 EVO-05.2c 身份/签名回执。EVO-05.3 才负责
rebase/revalidate，HAR-09.6 仍等待真实 promotion/rollback Outcome authority。
