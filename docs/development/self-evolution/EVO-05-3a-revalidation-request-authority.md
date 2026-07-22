# EVO-05.3a Revalidation Request Authority

## 1. 目标

把仍然 `current` 的 `approved` Approval Decision 与 exact Promotion Package 冻结为一个确定性、可持久化、
可动态失效的 Revalidation Request，作为未来隔离 rebase/revalidate executor 的唯一合法输入。

本切片只建立请求权威，不执行 checkout、worktree 创建、patch replay、rebase、测试、Git write、merge、push、
publish 或 Promotion。`ready` 表示请求的上游证据仍有效，不表示任何验证或发布动作已经发生。

## 2. 依赖与边界

上游必须同时满足：

1. Approval Decision artifact 可完整重验，状态为 `approved`，且当前 source-set 仍完全一致；
2. Promotion Package artifact、Promotion Input、Reflection 与 target 仍可审查；
3. Decision 与 Package 的 workspace、package、target head/tree 和摘要逐项相同；
4. Requirement 未过期，所需角色、Principal、Ed25519 key、签名回执与技术门仍 current。

本模块不读取模型叙事，不保存源码，不接受自由文本，不调用 LLM，也不把 `bypass` 解释为跳过 digest、签名、
target 或 workspace 不变量。Permission mode 只决定 Tool 是否可调用，不能改变 authority 结果。

## 3. Artifact 契约

`EvolutionRevalidationRequest` 使用 `evolution-revalidation-request-v1`，identity 为 canonical JSON 的 SHA-256：

- `request_id = evrevalidation_<digest[:24]>`；
- 绑定 Decision id/digest/source-set digest；
- 绑定 Requirement id/digest；
- 绑定 Package、Promotion Input、Candidate revision 与 Reflection id/digest；
- 绑定 target branch/head/tree、baseline commit 与 relation；
- 绑定 patch manifest、baseline、migration assessment、rollback plan digest；
- 冻结 required platforms；
- target 与 baseline 相同时操作为 `validate_exact_tree`，线性前进时为 `rebase_then_validate`，已经 diverged 时为
  `block_for_reconciliation` 并机械要求人工 reconciliation，不能把冲突偷换成自动 rebase；
- 固定要求 sandbox、exact target、current approval 和重新签发 validation receipts。

以下字段必须永久为 `false`：network、dependency install、main worktree write、target branch write、execution started、
rebase executed、validation executed、promotion authority、merge、push、publish、source-code payload、freeform narrative、
LLM generated。任何字段、ID 或摘要篡改都会使 Pydantic artifact 校验失败。

## 4. 持久化与并发

`EvolutionRevalidationRequestStore` 在用户状态库中使用 `evolution_revalidation_requests`：

- `request_id` 为主键，`decision_id` 唯一；
- 落库前在同一 SQLite write transaction 内重读完整 Decision/Package JSON artifact，而不只相信索引列；
- artifact 与 SQLite index 任一不一致均 fail closed；
- 同一 immutable Decision 的并发签发是幂等的，只产生一个 request；
- 同一 Decision 若计算出不同 request，返回 conflict，不能覆盖历史记录；
- 不合格 Decision 在 builder 阶段阻断，不创建 request 表或空记录。

外部 Git target 不能与 SQLite 形成跨系统原子事务，因此签发完成后 Service 会再次计算 current view；若 target 在
检查与落库之间移动，已落库 request 保留审计，但立即显示 `stale`，绝不会报告可执行。

## 5. 动态状态

`EvolutionRevalidationRequestView` 只允许三态：

| 状态 | 条件 | execution eligible |
| --- | --- | --- |
| `ready` | source 可读，Decision、Package、target 全部 exact/current | true |
| `stale` | source 可读，且 Package/Input/Reflection 或 target 已变化 | false |
| `ineligible` | source 不可读，或 target 仍相同但审批、签名、Principal、Requirement 已失效 | false |

状态优先级先判断 Package/target 漂移，再判断审批资格。因此 main 前进会明确显示 `stale`，Principal key 轮换或
Requirement 失效则显示 `ineligible`。上游 authority 无法读取时 fail closed 为不可执行。

## 6. 双通道入口

用户与 Agent 共享同一个 `EvolutionRevalidationRequestService`：

```text
/evolution revalidation-request <approval-decision-id>
/evolution revalidation-request show <revalidation-request-id>
```

- `evolution_revalidation_request`：签发 durable artifact，中风险，所有非 lockdown 模式可调用，最多 50 次，
  不做二次确认；
- `evolution_revalidation_request_authority`：只读重验当前状态；
- Engine composition、lazy public exports、Slash 和两个 Tool 不复制状态计算或持久化逻辑。

所有输出必须显式说明“只授权未来隔离输入”，并列出当前没有执行的 Git/Promotion 动作，防止 UI 把 request
误呈现为完成回执。

## 7. 验收证据

- 真实临时 Git repository、SQLite 与 Ed25519 keypair 完整构造 approved Decision，再签发 request；
- 8 路并发签发只保留一行，返回完全相同的 request/view；
- pending 与显式 reject Decision 都被阻断，且无持久化副作用；
- target commit 前进后历史 request 变为 `stale`；
- independent reviewer Principal key 轮换后 request 变为 `ineligible`；
- Request digest、SQLite index、完整 Package JSON 和 workspace mismatch 全部 fail closed；
- Slash、写 Tool、只读 Authority、Engine composition、lazy export 与 PermissionRule 使用同一实现；
- 定向 Ruff、compile 和 30 个相关测试通过；未运行全量测试。

## 8. 自我审视与剩余工作

本切片关闭了“approved Decision 如何安全进入再验证阶段”的 authority 缺口，但没有完成 EVO-05.3：

- EVO-05.3b 仍需创建隔离 worktree/replay executor，并证明绝不写 main worktree 或 target branch；
- EVO-05.3c 仍需把 request 映射到重新绑定的 Validation Plan/Eval cohort，并签发新 receipt；
- EVO-05.3d 仍需形成 Revalidation Outcome，比较 replay 后生产文件树并使旧 Eval receipt 确定性 stale；
- 任何 rebase conflict、validation failure、取消、崩溃恢复与清理都必须有 durable receipt；
- 完整 EVO-05 仍未实现 rollout、runtime monitor、rollback 与 Outcome authority。

因此后续不能直接 merge、push 或 promotion；下一开发切片只能消费 `ready` request，在隔离边界中完成可恢复 replay。
