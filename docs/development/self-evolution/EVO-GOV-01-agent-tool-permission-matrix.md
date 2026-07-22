# EVO-GOV-01 Evolution Agent Tool 权限矩阵

## 问题与目标

EVO-03.7a/3.7b1/3.7b2、EVO-04.1a 至 4.7a 与 EVO-05.1a 至 5.2d 已注册十九个非只读 Agent Tool。它们拥有真实的 durable
写入，但此前没有精确 `PermissionRule`，因此 normal runtime 将其判为 `UNKNOWN_TOOL`，Engine 的“所有注册
工具均受治理”门也会失败。

本基线为这些已经交付的 Tool 建立显式、可测试的权限矩阵。它不改变任何 artifact schema、issuer、executor、
Store、Slash 命令或后续 decision 语义。

## 风险分类依据

其中十七个 Tool 只能从已有 authority 派生并持久化不可变证据、决策、用户 Resolution、Reflection、
Promotion Input、review-only Package、不可执行 Approval Requirement 或 role response。新增 Principal 治理是
独立 `HIGH` authority 动作：它变更未来签名验证的可信身份、公钥或角色，但必须经过 HAR 人工确认，且绝不接收
私钥：

- 不运行项目代码或 Shell；
- 不修改 Candidate/worktree/main；
- 不扩大 Experiment scope、budget、network 或 dependency 权限；
- 只有 `evolution_decision_state` 可按固定机械 policy 标记 `accepted_experiment`，且仍不产生 promotion、
  baseline 更新或 Git 写入；其他 Tool 不接受 Candidate；
- 重复调用由各 Store/Executor 幂等或 single-flight 收敛。

Signature Tool 只创建 nonce Challenge 或验证外部 public signature；Approval Decision Tool 只聚合 current
authority 并保持所有 Git/Promotion 字段为 false。因此十七类
派生创建为 `MEDIUM`：高于只读查询，但不逐次要求确认。Reflection 撤销与 Principal 治理均为
`HIGH`：normal 由 PermissionChecker 确认；Principal 变更还必须完成 HAR durable interaction。bypass 按全权限
语义跳过 PermissionChecker 确认，但不会代答 HAR。

## 权限矩阵

| Tool | Artifact | Family | Session 上限 |
| --- | --- | --- | ---: |
| `evolution_evaluation_receipt` | 单 lane Evaluation Receipt | `evolution_evaluation_artifact` | 200 |
| `evolution_evaluation_contract` | Evaluation Aggregation Contract | `evolution_evaluation_artifact` | 50 |
| `evolution_final_evaluation_receipt` | Final Evaluation Receipt | `evolution_evaluation_artifact` | 50 |
| `evolution_decision_input` | Decision Input | `evolution_decision_artifact` | 50 |
| `evolution_mechanical_gate` | Mechanical Gate | `evolution_decision_artifact` | 50 |
| `evolution_independent_review` | Independent Review | `evolution_decision_artifact` | 20 |
| `evolution_counterfactual_evidence` | Counterfactual Evidence | `evolution_decision_artifact` | 50 |
| `evolution_reward_hacking_evidence` | Reward-hacking Evidence | `evolution_decision_artifact` | 50 |
| `evolution_decision_state` | Final Decision State | `evolution_decision_artifact` | 50 |
| `evolution_decision_resolution` | Escalation Resolution | `evolution_decision_artifact` | 50 |
| `evolution_reflection_memory` | Reflection Memory | `evolution_reflection_memory` | 50 |
| `evolution_revoke_reflection_memory` | append-only Reflection Revocation | `evolution_reflection_memory` | 20 |
| `evolution_promotion_package_input` | non-executable Promotion Input | `evolution_promotion_artifact` | 50 |
| `evolution_promotion_package` | exact-target review Package | `evolution_promotion_artifact` | 50 |
| `evolution_promotion_approval_requirement` | approval roles/signature gates | `evolution_promotion_artifact` | 50 |
| `evolution_promotion_approval_request` | HAR-fenced role response | `evolution_promotion_artifact` | 50 |
| `evolution_approval_principal` | HAR-fenced Principal/role/public-key authority | `evolution_approval_identity` | 20 |
| `evolution_approval_signature` | nonce Challenge / verified Ed25519 Receipt | `evolution_approval_signature` | 50 |
| `evolution_promotion_approval_decision` | append-only non-executable Approval Decision | `evolution_promotion_artifact` | 50 |

Independent Review 的上限更低，因为首次成功路径会调用 Reviewer 模型；durable single-flight 仍负责同一 Gate
并发去重，权限上限负责限制一个会话内不同 Gate 的总调用面。

## 模式语义

- permissive/moderate/strict：十七类派生创建允许且无逐次确认；Reflection 撤销和 Principal 治理允许但要求确认。
- lockdown：阻断所有十九类写入；已有只读 Authority Tool 仍按各自只读规则工作。
- bypass：全权限直接通过，不要求确认，也不受本层 session call cap 限制。

bypass 只绕过交互 PermissionChecker；executor 仍必须重读 authority、验证 workspace/identity/digest/budget，
Store 冲突与 mechanical veto 仍不可被绕过。

## 验收

- 十七个派生 Tool 在 permissive/moderate/strict 返回 `ALLOW + MEDIUM`，family 精确匹配；
- Reflection 撤销与 Principal 治理在 normal 返回 `ALLOW + HIGH + confirmation`，bypass 无 PermissionChecker 确认；
- lockdown 返回 `MODE_BLOCKED`；
- normal 模式达到各自上限后返回 `MAX_CALLS_EXCEEDED`；
- bypass 在超过同一上限后仍直接允许且无确认；
- `AgentEngine` 全注册表不存在未知非只读 Tool；所有 Tool schema 继续兼容 OpenAI function contract；
- 只运行权限与 Engine 注册表小模块，不以全量测试冒充本切片证据。

## 新 Tool 约束

以后任何 `metadata.read_only=false` 的 Evolution Tool 必须在同一提交中提供：精确 PermissionRule、风险依据、
允许模式、确认策略、调用上限、family、bypass/lockdown 测试和 authority 边界。依赖“未知 Tool 默认阻断”不是
完成权限设计。
