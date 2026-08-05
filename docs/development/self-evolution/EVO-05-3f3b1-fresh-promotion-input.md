# EVO-05.3f3b1 Fresh Promotion Input

## 目标

在 target 前进后的 Fresh Final Evaluation 与新一轮审批之间建立版本化、不可变的 Promotion Input。旧
`evpromoin_` artifact 仅作为 patch、baseline、migration 与 rollback 的只读来源，不能把旧 Final、Decision、Approval
或专业签名带回新的 authority chain。

## 真实数据链

`EvolutionRevalidationPromotionInputService` 每次签发前依次复验：

1. Fresh Runtime Contract 仍为 current/ready；
2. Validation Plan 与 Fresh Final Evaluation 均存在；
3. Fresh Reapproval Authority 可由当前 eligible Final 动态签发；
4. Validation Plan 指向的 prior Promotion Input identity/digest、candidate revision 与 required platforms 完全一致；
5. Store 在同一事务中重新检查 prior input、Fresh Final 与 Reapproval Authority 的持久化 identity/digest。

生成的 `evrevalpromoin_` artifact 嵌入旧 input 的完整只读快照，并明确冻结以下语义：

- `prior_final_invalidated=true`；
- `prior_decision_reusable=false`；
- `prior_approval_reusable=false`；
- `prior_signature_reusable=false`；
- `approval_requirement_ready=true`，但 `approval_decided=false`；
- `promotion_authority=false`。

prior Reflection 当前是否 active 不再赋予该 artifact 权威：其旧 Promotion Input 只用于保留已审计的 patch/rollback
事实。新鲜性和继续资格只来自当前 Runtime Contract、Fresh Final 与 Reapproval Authority。

## 失败关闭

- Contract stale 或不完整：`fresh_promotion_input_contract_not_ready`；
- 缺 Plan/Final：`fresh_promotion_input_authority_missing`；
- Fresh Final 不允许 reapproval：`fresh_promotion_input_reapproval_blocked`；
- 缺 prior input：`fresh_promotion_input_prior_missing`；
- 任一 cross-artifact binding 不一致：`fresh_promotion_input_authority_mismatch`；
- Store 中任一依赖缺失或 digest 漂移：`fresh_promotion_input_dependency_mismatch`；
- 同一 Contract 试图覆盖不同 input：`fresh_promotion_input_conflict`。

## 验收标准

- 真实 Fresh cohort/matrix/final 场景中，负向 Final 无法生成 Promotion Input；
- eligible Final 只能生成一个确定性、可重放的 `evrevalpromoin_` artifact；
- artifact 精确绑定 Final 与 Reapproval Authority，并保留 prior patch/rollback 输入；
- 删除 prior input 持久化依赖后，即使内存中仍有完整 artifact，Store 也必须拒绝写入；
- Engine 暴露 store/service，lazy public exports、定向 Ruff、聚焦 pytest、import 与 YAML 验证通过；
- 不授予 approval decision、promotion、merge、push 或 publish authority。

## 后续依赖

EVO-05.3f3b2 将基于本 artifact 生成新的 Approval Requirement。随后分别实现新的 durable role interaction、签名
challenge、Decision 聚合与 promotion gate；这些步骤均不得复用旧 Approval receipts。
