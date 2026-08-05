# EVO-05.3f3b2 Fresh Approval Requirement

## 目标

将版本化 Fresh Promotion Input 转换为不可执行、可签名、不可复用旧响应的审批要求。该 artifact 不接受已经失效的
旧 `evpromopkg_` 或旧 Reflection active 状态作为继续权威。

## Authority 链

服务在每次签发前重新取得 Fresh Promotion Input，并交叉验证：

1. current Fresh Runtime Contract 与 Validation Plan；
2. eligible Fresh Final Evaluation 与 Reapproval Authority；
3. validated Revalidation Outcome；
4. Outcome 指向的原 Revalidation Request，用它恢复精确 target branch；
5. Validation Plan 的 RED revision/tree，用它替代已过时 Request 中的 target snapshot；
6. prior input 的 patch、baseline、migration 与 rollback digest。

因此 target branch 来自真实历史操作意图，target head/tree 来自 current revalidation 证据。实现不默认猜测 `main`，也不
把 target 前进前的 head/tree 带入新审批。

## 审批策略

- Reapproval Authority 要求的 user、independent reviewer、security reviewer、release manager 全部进入新 steps；
- migration review 或 data backup 存在时额外要求 data owner；
- user 提供 durable consent，所有专业角色都必须重新提交签名；
- 每个 step 均声明 `fresh_interaction_required=true`、旧 response/signature 不可复用；
- signable payload 绑定 Fresh Input、Contract、Final、Reapproval Authority、current target 与 patch/rollback；
- Requirement 自身仍为 review-only，不创建交互、不作决定、不授予 promotion/Git/publish authority。

## Store 失败关闭

单次 SQLite 事务内复验 Fresh Input JSON 以及 Input、Final、Validation Plan、Reapproval Authority、Outcome、Request 的
identity/digest 和关键索引。删除或替换任一依赖均以 `fresh_approval_requirement_dependency_mismatch` 拒绝落库；同一
Fresh Input 不允许覆盖为不同 Requirement。

## 验收标准

- 真实 Fresh cohort/final/input 链可生成确定性、幂等 Requirement；
- target branch 与 current revision/tree 来自不同但可验证的 authoritative source；
- migration/data-backup 场景必须包含 data owner，专业角色签名 quorum 与 steps 完全一致；
- 删除 Request 等 current-target 依赖后 Store 失败关闭；
- public exports、Engine composition、定向 Ruff、聚焦 pytest、import 与 YAML 通过；
- 不得复用旧 Decision、Approval、Response 或 Signature。

## 后续依赖

EVO-05.3f3b3 将按本 Requirement 为每个角色创建新的 durable interaction/response authority。EVO-05.3f3b4 再生成
绑定本 `signable_payload_sha256` 的全新专业签名 challenge；之后才能聚合新 Decision。
