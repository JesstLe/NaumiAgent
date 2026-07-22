# EVO-05.2d Approval Decision Aggregation

## 目标与非目标

本切片把一个 exact Approval Requirement 下的全部角色响应、专业身份、Ed25519 Signature Receipt、Promotion
Package 与技术门聚合成可重放、append-only 的 `EvolutionPromotionApprovalDecisionReceipt`。它回答的是：

> 在某个确定时刻，这组 durable authority 能否形成 approved、rejected、changes_requested、pending 或 stale？

即使结果为 `approved`，也只设置 `rebase_revalidation_eligible=true`。Decision 永远固定声明
`promotion_authority=false`，并且不会执行 rebase、测试、Git write、merge、push、publish 或 Promotion。

## Authority 输入

每次聚合必须从 Store 和动态探针重读，不能相信模型上下文或调用方摘要：

1. exact Approval Requirement ID/digest、policy、steps、technical gates 与 expiry；
2. exact Promotion Package ID/digest、Input/Reflection active 状态和当前 target head/tree；
3. 每个 required role 唯一的 immutable Approval Response；
4. 每个要求签名且回答 `approve` 的角色对应唯一 Signature Receipt；
5. Signature Authority 动态重查的 current Principal event、active state、role、key ID/generation、Requirement
   expiry 与 target；
6. 前一个 Decision ID/digest，形成按 Requirement 隔离的 append-only hash chain。

额外 Response、重复角色、重复 Signature、属于其他 Response 的 Signature、digest/index 不一致或跨 workspace
输入均 fail closed。legacy Requirement v1 保持可读，但未要求签名的专业角色 Response 仍是
`identity_unverified`，不能因历史兼容而计入 quorum。

## 角色与技术门投影

每个 required role 只产生以下确定性 outcome：

- `approved`：local session user 已验证，或专业角色拥有 current eligible Signature Receipt；
- `rejected` / `changes_requested`：结构化负向回答，不要求先补签名；
- `response_missing`：尚无该角色回执；
- `identity_unverified`：legacy/不可信专业角色身份未绑定；
- `signature_missing`：专业角色 approve，但没有 Signature Receipt；
- `signature_stale`：历史签名真实存在，但 Principal、key generation、role、Requirement 或 target 已变化。

技术门同时投影为 `satisfied|blocking|stale`。Package/Input/Reflection/target 必须 current；migration review 与
data backup 只能由已验证的 `data_owner` approval 提供证据；`rebase_required` 和 `revalidation_required` 在
EVO-05.3 未交付前始终 blocking。

## 决策优先级

状态按固定优先级计算，不调用 LLM：

1. Package/Input/Reflection/target 非 current、Requirement expired：`stale`；
2. 任一 `reject`：`rejected`；
3. 任一 `request_changes`：`changes_requested`；
4. 任一签名变 stale：`stale`；
5. 缺 Response、身份、签名或存在 blocking gate：`pending`；
6. 所有 required role 与 technical gate 均满足：`approved`。

负向决定优先于缺失角色，使明确拒绝不会被错误渲染为“仅待补材料”；但任何 stale source 都优先于历史负向
回答，避免对已经脱离当前 target 的 Package 作出看似 current 的治理声明。

## Receipt、幂等与并发

Receipt 包含 source-set digest、Decision digest/ID、单调 sequence、previous Decision ID/digest、角色/技术门投影、
计数、missing roles、blocking gates 与全部不可执行边界字段。source-set digest 不含聚合时间和 chain link，因此
同一 authority 快照的重复/并发调用幂等返回同一历史 Receipt，而 authority 变化会追加新 sequence。

Service 在写入前采集两次 source-set；变化则重试。Store 使用 `BEGIN IMMEDIATE`，事务内再次核对 Requirement、
Package、Response、Signature 和正向 quorum Principal latest-index，再验证 chain head 和唯一 source-set。落库后
Service 第三次重读动态 authority，返回真实 `source_current/current_status`；因此即使 authority 恰好在 commit 后
轮换，也不会把历史 Receipt 错报为 current。

## 双通道与 UI/TUI

用户手动通道：

```text
/evolution approval-decision <approval-requirement-id>
/evolution approval-decision show <approval-decision-id>
```

Agent 通道：

- `evolution_promotion_approval_decision`：写入 append-only Decision Receipt；
- `evolution_approval_decision_authority`：只读重载历史 Receipt 并动态检查 current 状态。

两条通道调用同一个 Engine Service。New UI 把命令交给共享 Slash channel；Textual TUI 复用同一个 slash router，
不存在第二套聚合状态。输出明确展示历史/current status、source current、approvals/signatures、missing roles、
blocking gates，并持续显示 Git/Merge/Push/Publish/Promotion 为 false。

## 权限与安全边界

写 Tool 为 `MEDIUM`，family=`evolution_promotion_artifact`，normal session 上限 50；
permissive/moderate/strict 无额外 PermissionChecker 确认，lockdown 阻断，bypass 直接通过且不受本层调用上限。
所有模式仍必须通过 digest、workspace、expiry、signature、Principal、target、SQLite index 和 chain 不变量。

## 验收证据

- 无响应 → 全响应但缺签名 → 全部真实 Ed25519 签名，形成 sequence 1/2/3 的 pending/pending/approved chain；
- 8 路同 source 并发聚合只保留一个 Receipt；
- reject 与 request_changes 在其他角色缺失时仍形成明确负向决定；
- Principal key rotation、target movement 与 Requirement expiry 使历史 approved 动态变 stale；
- legacy v1 未签名专业身份不能计入 quorum；
- 重复/非 scoped Signature、Receipt JSON/SQLite index 篡改 fail closed；
- Slash、写 Tool、只读 Authority、Engine composition、lazy export、PermissionRule 与 New UI shared route 均有
  focused tests；
- 真实临时 Git repository、SQLite 与 Ed25519 keypair 场景通过；未运行全量测试。

## 自我审视与剩余边界

本切片完成了“审批证据聚合”，没有完成“批准后的执行”。当前 Signature Store 对一个 immutable Response 只允许
一个成功 Receipt；若其 Principal key 后续轮换或撤销，历史 Decision 会正确变 stale，但同一 Response 不能用新 key
覆盖签名。恢复路径是生成新的 current Package/Requirement/Response 审批周期，而不是改写历史证据。未来若需要在
同一 Requirement 内显式重签，必须设计新的 Response revision 与 supersession authority，不能放宽现有唯一约束。

下一步不能直接 promotion。EVO-05.3 需要消费 current approved Decision，建立隔离的 rebase/revalidate authority，
重放 patch、重新运行绑定验证，并让 target 或验证输入变化确定性使结果失效。完整 EVO-05 仍依赖 ARC-07 的打包与
发布边界。
