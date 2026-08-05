# EVO-05.3f3b3 Fresh Role Interactions and Responses

## 目标

把 Fresh Approval Requirement 的每个角色 step 转换为 HAR-10.6 持久交互，并冻结全新的 role-scoped Response
authority。旧 Approval Response、旧 interaction 与旧签名均不能进入本链路。

## 执行链

`EvolutionRevalidationApprovalRequestService` 每次执行前重新签发并核对 current Fresh Requirement，然后：

1. 按 `contract + requirement + role` 建立进程内 singleflight；
2. 从 Harness Store 读取 exact Requirement/Role 的历史交互；
3. 已回答交互只能有一个，pending interaction 不重复创建，cancelled/expired 才递增 attempt；
4. 通过统一 `request_user_input` callback 创建 HAR-10.6 durable interaction；
5. 回调返回后从 Harness Store 重读 terminal authority；
6. 再次动态复验 Requirement current/expiry；
7. Store 重读 Requirement 与 Harness terminal record 后持久化 Response receipt。

交互只允许 `approve`、`request_changes`、`reject` 三个结构化选项，不允许自定义审批文本。所有问题明确说明该回答不会
执行 Git、merge、push 或发布。

## 身份与 quorum

- user 必须绑定非空本地 session，approve 后才可计入 role quorum；
- 专业角色的 UI 回答只是 `unverified_role_claim`，即使 approve 也不能直接计入最终聚合；
- 专业角色 approve 只产生 `eligible_for_fresh_signature=true`，后续仍需新 Principal/Signature authority；
- Response 绑定 Fresh Requirement 的 signable payload，并声明旧 response/signature 均未复用；
- reject/request_changes 永不开放签名或聚合资格。

## 持久化防线

- interaction ID 绑定 exact Requirement digest suffix、role 与 attempt；
- receipt 绑定 Requirement/Input identity、step order/reasons、interaction request digest 与 terminal interaction digest；
- Store 读取和重复调用均重新核对 Harness authority；
- 同一 Requirement/Role 只能有一个不可变 Response；
- receipt 不授予 Decision、promotion、Git 或发布 authority。

## 验收标准

- 8 路并发 user 请求只创建一个 durable interaction 和一个 receipt；
- user approve 可计入 role quorum，但不形成整体 Decision；
- professional approve 只开放新签名资格，身份/签名未完成前不可聚合；
- Requirement stale/expired、pending/ambiguous interaction、未提交答案或依赖漂移均失败关闭；
- 定向 Ruff、聚焦 pytest、Engine/public import 与 YAML 通过；不运行全量测试。

## 后续依赖

EVO-05.3f3b4 将为 approve 的专业 Response 创建全新 Principal 与 Ed25519 signature challenge/receipt，并绑定本轮
Requirement 的 `signable_payload_sha256`。完成所有角色后，EVO-05.3f3b5 才能聚合新 Decision。
