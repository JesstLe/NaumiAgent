# EVO-05.1a Promotion Package Input Contract

## 目标与边界

本切片把通过 EVO-04 的实验结果冻结成后续 promotion review 唯一可接受的结构化输入。入口只接受
`accepted_experiment` 且仍 active 的 Reflection Memory；它不审批 Candidate，不生成完整 Promotion Package，
不修改工作区，也不执行 Git、merge、push、publish、migration 或 rollback。

## Authority 链

`active Reflection → Decision State → Decision Input → Experiment/Mutation/Final Evaluation`

Executor 只接收 Reflection ID，并从 durable Store 重读 exact Reflection 与 Decision。Builder 再对 workspace、
candidate revision、risk、decision ID/digest、accepted 状态、lesson/action 和 promotion readiness 做全量交叉验证。
Store 使用与 Reflection 相同的 SQLite authority 库，在 `BEGIN IMMEDIATE` 事务内验证 Reflection 行及撤销表，避免
“检查 active 后、写入前被撤销”的竞态。

## 冻结内容

- Patch manifest：排序后的文件、modify/create、before/after/diff digest、行数、API change 和 Mutation fact digest；
- Baseline：commit、dirty-at-issue、source snapshot、tree、Harness profile、experiment config、toolset digest；
- Evidence refs：Experiment、Snapshot、Mutation、Validation Plan、Aggregation、Final Receipt，每条 Lane 的
  RED/GREEN/Comparison/Attribution，以及 Gate、Review、Counterfactual、Reward、Decision、Reflection；
- Migration assessment：数据库迁移、状态 schema、依赖清单与 additive API 的确定性路径信号；
- Rollback plan：modify 文件恢复 baseline blob，create 文件删除；只形成计划，不授予执行权；
- Risk、required platforms 与 lane count。

Artifact 不保存源码、Reviewer 叙事或用户自由文本，不调用 LLM。所有嵌套对象和顶层对象均有 canonical SHA-256；
固定布尔位声明 package/approval/promotion/Git/merge/push/publish 均未发生。

## 撤销语义

Input 是 append-only 审计记录。Reflection 后续被撤销时不删除 Input，但每次读取都会联合检查 Reflection authority，
动态返回 `reflection_active=false` 与 `promotion_review_eligible=false`。已撤销 Reflection 不能新写或覆盖 Input。

## 双通道与权限

- 用户：`/evolution promotion-input <reflection-id>`；
- Agent Tool：`evolution_promotion_package_input`；
- 两者共用 `EvolutionPromotionPackageInputExecutor`；
- permissive/moderate/strict 为 `MEDIUM`、无需逐次确认、每会话 50 次；lockdown 阻断；bypass 直接通过；
- New UI 将命令透传共享 Slash channel，TUI 复用同一 Slash router。

## 验收证据

- 真实 SQLite 写入 active accepted Reflection 与 Input，重复读取保持 exact artifact；
- 撤销 Reflection 后 Input 仍可审计但立即失去 review eligibility，重复写入被拒绝；
- 即使重算顶层摘要，也无法把 Git execution 等固定边界改为 true；
- migration 与 rollback 由真实 patch facts 确定生成，schema/state 变更要求备份审查；
- Agent Tool、Slash、新 UI 透传、权限和 Engine composition 有聚焦测试；
- Ruff、py_compile 与上述小模块测试通过，不以全量测试代替本切片证据。

## 后续依赖

EVO-05.1b 已从仍 eligible 的 Input 构造 exact-target review Package，并补齐 approval policy 所需签名域和
目标分支信息。EVO-05.2a Approval Requirement 也已完成，下一最小依赖是 EVO-05.2b；当前仍不得直接 merge/push，HAR-09.6 Outcome authority 继续等待
显式 promotion/rollback executor。
