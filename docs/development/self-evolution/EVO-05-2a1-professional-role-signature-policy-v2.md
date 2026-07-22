# EVO-05.2a1 Professional Role Signature Policy v2

## 修复目标

EVO-05.2d 前置审计发现 Approval Requirement v1 存在不可满足的身份闭环：EVO-05.2b 只把 `user` 角色绑定到
本地 session user，所有专业角色 Response 都是 `unverified_role_claim`；但 v1 对 medium/部分 protected scope
的 `independent_reviewer` 不要求签名。因此该角色既不能以 Response 身份计入 quorum，也没有 Signature Receipt
入口，严格聚合永远无法批准。

本切片不在聚合器里信任角色自称，而是发布
`evolution-promotion-approval-requirement-v2`：所有非 `user` required role 都必须通过 EVO-05.2c1 Principal
authority 与 EVO-05.2c2 真实 Ed25519 Signature Receipt 完成身份绑定。

## 策略语义

- `user`：HAR durable interaction + non-empty local session identity，不要求密码学签名；
- `independent_reviewer`、`security_reviewer`、`data_owner`、`release_manager`：HAR durable interaction 后必须
  由具备 exact role 的 current active Principal key 签署 exact Response Challenge；
- `minimum_approvals` 仍等于全部 required roles；
- `minimum_signatures` 等于 required roles 中除 `user` 外的数量；
- bypass 不能降低专业角色签名要求；
- reject/request-changes 仍是安全阻断信号，不会因缺少正向身份凭据而被忽略。

## 向后兼容

模型继续接受并完整校验 v1 artifact；Store 以 `(package_id, policy_version)` 保留 v1/v2 两份不可变 authority，
不会覆盖历史 Requirement。`get(requirement_id)` 可重载任一版本，新 `execute(package_id)` 只查找或签发 current
v2。

Store 重放 v1 时使用 v1 确定性 Builder，重放 v2 时使用 v2 Builder；调用方不能把 v1 的 role/signature projection
改写成 v2，也不能让新执行回退到 legacy policy。现有 v1 Response/Signature 审计事实保留，但 EVO-05.2d 对
缺少专业角色可验证身份的 legacy Requirement 必须返回 pending/stale，不得宣称 approved。

## 验收证据

- medium persistence Requirement 的 `independent_reviewer`、`data_owner`、`release_manager` 全部需要签名；
- low authorization scope 的 independent/security/release 全部需要签名；
- low 且无专业角色时仍只有 user、零签名；
- 同一 Package 的 v1 与 v2 Requirement 可同时持久化、分别重载且 ID/digest 不同；
- 8 路 current execute 仍 single-flight 到同一 v2 Requirement；
- v1/v2 policy projection、角色数、签名数和 Store index 篡改继续 fail closed；
- Requirement、Response、Principal、Signature 聚焦回归通过；未运行全量测试。

## 边界

本切片只关闭专业角色的正向身份验证缺口，不聚合决定、不创建 Principal、不持有私钥、不执行 Git。下一步
EVO-05.2d 必须只把 local user approval 或 current eligible Signature Receipt 计入正向 quorum。
