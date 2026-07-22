# EVO-05.2a Approval Requirement Policy

## 目标与边界

本切片只从 still-current EVO-05.1b Package 计算并冻结审批要求：谁必须审批、谁必须签名、哪些技术门必须先完成、
要求何时过期。它不创建 HAR-10.6 interaction，不接受用户答案，不选择 signer 或读取密钥，不作出 approval
decision，也不执行 rebase、Git、promotion、migration 或 rollback。

## Authority 链

`active Input/Reflection → still-current exact-target Package → Approval Requirement`

Executor 在签发前通过 Package Executor 动态复核 Input/Reflection、target head/tree 与 Package eligibility。Store
写入前独立重跑只读 target probe，并在共享 SQLite 的 `BEGIN IMMEDIATE` 内重读 exact Package、Input 与
Reflection revocation。`(package_id, policy_version)` 跨进程单飞；并发调用的首个 issued/expires 时间成为
authority，后续只有 policy projection 完全一致才幂等返回。
Store 还会以 artifact 的 `issued_at` 重新运行当前确定性 Builder，并要求整个 Requirement 完全一致；调用方即使
重算所有摘要，也不能删除 reviewer、降低签名数或移除 technical gate。

## 角色策略

| 条件 | 新增角色 | 签名要求 |
| --- | --- | --- |
| 所有 Package | `user` | durable interaction，非密码学签名 |
| medium/high/critical 或 protected scope | `independent_reviewer` | high/critical 需要签名 |
| critical 或 authorization/security scope | `security_reviewer` | 需要签名 |
| persistence、migration review 或 data backup | `data_owner` | 需要签名 |
| protected target、CI/release/dependency scope 或 high/critical | `release_manager` | 需要签名 |

所有角色均为 required、human-required；`minimum_approvals` 等于角色数，不允许模型或 bypass 自动批准。
protected scope 机械设置 `protected_scope_human_gate=true`，任何后续策略都不能把它降级为自动批准。

## 技术门与有效期

基础门固定包括 Package current、Input active、Reflection active、Target current。advanced/diverged Package 追加
`rebase_required` 与 `revalidation_required`，并设置 `approval_request_ready=false`；EVO-05.3 完成前甚至不能创建
审批请求。migration/data-backup 作为必须提供证据的审批门，但不会绕过对应 data owner。

- low：7 天；
- medium：3 天；
- high：24 小时；
- critical：6 小时；
- migration 最长 24 小时；需要 rebase 时最长 6 小时。

到期、target 移动或 Reflection 撤销不删除 Requirement，但动态设置 `approval_request_eligible=false`。

## Artifact 与防篡改

Requirement 保存 exact Package/Input/Reflection/target/signable digest，排序后的 role steps、reason codes、签名角色、
quorum、technical/blocking gates、UTC issued/expires 与 policy projection digest。固定：

- `approval_decided=false`；
- `signatures_collected=false`；
- `interaction_created=false`；
- `promotion_executed=false`、`git_write_executed=false`；
- 不保存自由文本、用户自定义输入或 LLM 叙事。

顶层 canonical SHA-256 与 stable ID 覆盖全部字段；Store 同时校验 JSON 与索引列。

## 双通道与权限

- 用户：`/evolution approval-requirement <promotion-package-id>`；
- Agent Tool：`evolution_promotion_approval_requirement`；
- 两者共用同一 Executor；New UI 透传共享 Slash channel，TUI 复用同一 router；
- permissive/moderate/strict 为 `MEDIUM`、无需逐次确认、每会话 50 次；lockdown 阻断；bypass 直接通过；
- bypass 只绕过交互 PermissionChecker，不能绕过 human gate、role/signature policy、expiry 或 authority 复核。

## 验收证据

- 真实 Git + SQLite：8 路并发只形成一个 Requirement；不同 issue time 仍返回首个 authority；
- medium persistence/main 映射为 user、reviewer、data owner、release manager 和两个签名；
- critical 增加 security reviewer，并要求 reviewer/security/data/release 四个签名；
- low authorization scope 仍强制 independent/security/release human roles 与 security/release 签名；
- expiry、target move、advanced rebase gate、Reflection revocation 全部 fail closed；
- 重算 digest 也不能伪造 approval/signature/Git execution；Store 索引篡改被检测；
- Slash/Agent Tool/New UI/TUI、权限、Engine composition 与 lazy exports 有聚焦测试；
- Ruff、py_compile 与小模块测试通过，不运行全量测试。

## 后续依赖

[EVO-05.2b](EVO-05-2b-approval-request-authority.md) 已把 still-eligible Requirement 的单个 role 映射为
HAR-10.6 fenced durable interaction，冻结 approval/rejection/request-changes Receipt，并为每个角色定义独立
signature entry。EVO-05.2c1 Principal/Public-Key Authority 已完成；下一步是 EVO-05.2c2 签名回执；最终 Decision aggregation 仍属于独立
EVO-05.2d，任何用户回答都不能直接 merge/push。
