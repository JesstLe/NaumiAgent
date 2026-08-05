# EVO-05.3f3a Fresh Reapproval Authority

## 目标

在 Fresh Final Evaluation 与 Promotion/Approval 之间建立独立、动态、fail-closed 的重新审批门禁。该门禁不放宽旧
`evfinal_` 类型，也不允许复用 target 前进前的审批或签名。

## 规则

- 每次签发前动态复验 Fresh Runtime Contract 和 current source；
- Fresh Final Evaluation 必须与 Contract 完全一致且 `reapproval_eligible=true`；
- 明确声明 `prior_approval_reusable=false`、`prior_signature_reusable=false`；
- user、independent reviewer、security reviewer、release manager 必须重新参与；
- 必须创建新的 durable interaction，专业角色必须重新签名；
- authority 只允许进入后续 Approval Input 构建，不授予 approval decision 或 promotion authority；
- Store 原子复验 Fresh Final Evaluation 的持久化 identity/digest。

## 验收标准

- 完整但负向的 Final Evaluation 必须以 `fresh_reapproval_evaluation_blocked` 阻断；
- stale Contract、缺 Final、Final/Contract 漂移或 Store 依赖缺失必须失败关闭；
- eligible Final 才能签发，重复签发幂等；
- authority 明确禁止旧审批和旧签名复用；
- 定向 Ruff、真实负向/正向 Final 场景、引擎 import 和 YAML 通过。

## 后续依赖

EVO-05.3f3b 将让版本化 Promotion Input/Decision/Approval Requirement 消费本 authority，并创建新的角色交互与签名
challenge。旧 Promotion Package 和 Approval receipts 只保留审计用途，不进入新聚合。
