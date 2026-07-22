# EVO-05.2b Approval Request Authority

## 目标与边界

本切片把 still-eligible EVO-05.2a Approval Requirement 的一个 required role 映射为真实 HAR-10.6
durable interaction，并把 fenced 结构化答案冻结为不可变 Approval Response Receipt。请求与回答可经
`/evolution approval-request <requirement-id> <role>` 或 Agent Tool
`evolution_promotion_approval_request` 进入同一 Service。

本切片不聚合最终 approval decision，不采信未验证的专业角色身份，不收集密码学签名，不执行 rebase、Git、
merge、push、publish 或 Promotion。`bypass` 只跳过工具 PermissionChecker 的二次确认，不能替用户选择答案，
也不能绕过 HAR fencing、Requirement eligibility、角色绑定或签名门。

## Authority 链

`active exact-target Package → still-eligible Approval Requirement → role step → HAR-10.6 interaction → Approval Response Receipt`

Service 每次执行都动态重读 Requirement View，拒绝已过期、target 已移动、Reflection 已撤销、Package 已失效或
仍有 rebase/revalidation blocking gate 的请求。每个角色使用稳定 interaction identity：

`ask-evapproval-<requirement-suffix>-<role>-<attempt>`

- pending interaction 不重复显示；
- cancelled interaction 可生成下一个单调 attempt；
- answered interaction 直接重读并幂等形成同一 Receipt；
- 同一 Requirement/Role 出现多个 answered authority 时 fail closed；
- `(requirement_id, role)` 在 Response Store 中唯一，任何不同答案都拒绝覆盖。

HAR-10.6 保证 create-before-display、answer-before-release、owner/epoch/sequence fencing、timeout、重启重放和
append-only event hash chain。Response Store 在写入前重读 exact terminal interaction，并验证其完整 digest；
因此调用方不能仅伪造 callback 返回值形成回执。

## 交互合同

每个请求固定三个选项：

1. `approve`：记录本角色同意意见；
2. `request_changes`：要求修改后重新审查；
3. `reject`：拒绝本次 Promotion。

审批交互禁止自定义文本，避免自然语言、Prompt injection 或秘密进入 promotion authority。问题显示 exact Package、
target head、role 和结构化 reason codes；timeout 不超过 Requirement 的原始有效期。答案必须在 Requirement expiry
之前写入 HAR authority，否则不能形成 Receipt。

## 身份与签名语义

HAR-10.6 当前能证明本地会话中的 `user` 回答了问题，但不能证明同一操作者确实拥有
`independent_reviewer`、`security_reviewer`、`data_owner` 或 `release_manager` 身份。因此 Receipt 明确记录：

- `user`：`identity_assurance=local_session_user`，角色绑定可验证；在 response 为 `approve` 且无需签名时，
  才可标记 `counts_toward_role_quorum=true`；
- 专业角色：`identity_assurance=unverified_role_claim`，即使选择 approve 也不能计入 quorum；
- 每个角色都带独立 `EvolutionPromotionSignatureReceiptEntry`；它冻结 role、是否必须签名、exact
  `signable_payload_sha256`、identity binding requirement，并固定 `signature_collected=false`；
- 任何 Receipt 都固定 `overall_approval_decided=false`、`final_quorum_reached=false`、
  `promotion_authority=false`、`git_write_executed=false` 和 `promotion_executed=false`。

这让后续身份/签名 authority 有明确入口，同时避免把“界面上点了同意”误报为安全审查或发布批准。

## Store 与防篡改

`evolution_promotion_approval_responses` 保存 Receipt identity/digest、Requirement/Package/Role、terminal interaction
identity/digest、response 和 recorded time。完整 Receipt 使用 canonical SHA-256 和 stable ID；读取时同时校验 JSON
模型与所有索引列。Receipt 内嵌 terminal `HarnessInteractionRecord`，但事实来源仍是 Harness Store，写入前必须
exact reread。

Receipt 不保存源码、自由文本、用户自定义输入、签名值、密钥或 LLM 叙事。签名 entry 只保存未来验证所需的
非秘密结构化字段。

## 双前端与权限

- New UI：`approval-request` 保持在共享 Slash channel；HAR-10.6 interaction card 负责上下键选择、timeout、
  replay 与 resolved 状态；
- Textual TUI：复用同一个 slash router、Engine Service 和 durable interaction adapter，无第二套审批状态机；
- Agent Tool：`evolution_promotion_approval_request` 与 Slash 使用同一 Service；
- permissive/moderate/strict/bypass 均允许，无二次确认，每会话最多 50 次，风险级别 medium；lockdown 阻断；
- bypass 不自动回答，也不改变 Receipt 的 identity/signature/quorum 固定字段。

## 验收证据

- 真实 Git + SQLite + Harness：8 路并发只显示一次、只形成一个 interaction 和一个 Receipt；
- create/answer 使用真实 `DurableInteractionAuthorityClient`，Receipt 只在 answered authority 可重读后生成；
- pending 重用、cancelled attempt 递增、target move 后动态失效；
- user approve 可形成 local-session role response；data owner approve 仍因身份未验证和签名缺失不能计入 quorum；
- custom answer、过期 Requirement、无效角色、重新计算 promotion authority 与 Store 索引篡改全部 fail closed；
- Slash、Agent Tool、Engine composition、lazy exports、权限表、New UI shared Slash channel 均有聚焦测试；
- Ruff、py_compile 与小模块 Python/Node 测试通过；未运行全量测试。

## 当前不足与下一步

EVO-05.2c1 已交付独立 trusted Principal/role/Ed25519 public-key authority，包括 HAR 人工注册、角色更新、公钥
轮换、撤销与 append-only hash chain；私钥不进入 Naumi。下一步 EVO-05.2c2 应把 role response 绑定 current
active key，验证 domain-separated payload、expiry 与 replay protection。随后另开 EVO-05.2d 做
Approval Decision aggregation，必须动态重查 Requirement、target、所有 technical gates、role identity 和签名回执；
聚合通过仍只授予进入 EVO-05.3 rebase/revalidate 的资格，不直接执行 Git 或发布。
