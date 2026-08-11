# UI-10.6b2 Workbench Proposal 合并交互

## 1. 目标

在既有 Workbench Reviews 页为 open Evolution Proposal 增加 `merge` 用户入口，使旧 revision 可以
显式收口到同一 Candidate 的较新 open Proposal。New UI 与 Textual TUI 只负责选择和提交目标；候选
过滤、权限判断、最终合法性校验、CAS 状态转换和审计继续由 Python authority 负责。

本动作只把源 Proposal 标记为 `merged` 并记录 `merged_into_id`。它不修改目标 Proposal，不执行代码、
不签发 Experiment Contract、不运行评测、不写 Git，也不授予 promotion 权限。

## 2. 依赖与唯一权威

```text
WorkbenchStore proposals
        |
        v
eligible_proposal_merge_targets()     bounded read projection
        |
        +--> Workbench Snapshot proposal.merge_target_ids (max 20)
        |
        v
New UI / Textual TUI target selection
        |
        v
PermissionChecker(workbench_govern_proposal)
        |
        v
WorkbenchService.govern_proposal(MERGE)
        |
        +--> reload source and target in current session
        +--> validate_merge_target() final revalidation
        +--> source-state CAS open -> merged
        +--> proposal.merged audit event
        +--> authoritative Workbench Snapshot refresh
```

`merge_target_ids` 是方便 UI 选择的有界只读投影，不是写权限，也不是最终准入凭证。目标可能在用户
选择后发生变化，因此 Service 必须重新读取并机械重验，前端不得凭快照直接改状态。

## 3. 合法目标不变量

一个目标只有同时满足以下条件才进入投影并可在最终写入时通过：

1. 源 Proposal 当前为 `open`；
2. 源与目标 ID 不同且属于同一 session；
3. 两者均来自 `evolution_candidate`；
4. 两者 `source_id` 相同；
5. 目标 `source_revision` 严格高于源；
6. 目标当前仍为 `open`。

目标按 `source_revision`、`created_at`、`id` 倒序稳定排列，去重后最多返回 20 项。helper 的显式
`limit` 只允许严格整数 `1..50`；关闭源、错误 Candidate、旧/相同 revision、关闭目标和重复 ID 均不
进入投影。Dashboard 当前最多读取 200 个 Proposal，因此 UI 投影是有界工作集；直接 Service 调用仍按
指定目标执行完整校验。

## 4. Typed 协议

复用已协商的 `workbench_proposal_actions` 能力与既有事件：

- client：`workbench/proposal/action`，新增 `action=merge`；
- merge 必须携带 `merge_into_id`，长度为 1..128，拒绝 NUL/CR/LF 和自身 ID；
- 非 merge action 禁止夹带 `merge_into_id`；
- server：`workbench/proposal/action_result`，允许 `action=merge`；
- completed merge 必须返回同一个源 Proposal，且其状态为 `merged`、`merged_into_id` 非空；
- Proposal Snapshot 的 `merge_target_ids` 最多 20 个、不得重复、不得包含控制字符或超长 ID；
- 未携带该 additive 字段的旧 Snapshot 在 Node 端兼容为空列表，不会误开放动作。

Python 协议边界先规范化客户端输入；Bridge 只接受当前 session，并把目标纳入权限参数。Node 协议边界
严格验证 completed result 的源对象绑定，拒绝伪造 open/空目标/错误源 ID 的成功回执。

## 5. New UI 交互

### Normal / permissive / moderate / strict

```text
m -> target list -> Enter -> confirm -> loading -> authority result
```

- 仅 open Proposal 显示 `m 合并`；没有 authority target 时显示中文原因且不发送事件；
- `↑/↓` 在最多 20 个目标中选择，列表显示 revision、标题和 ID；
- Enter 固定选中目标，确认页显示精确 `merge_into_id`，`y`/Enter 提交，Esc 取消；
- loading 阶段阻止重复提交；成功后以返回的权威 Snapshot 替换页面状态；
- 80/120/200 列都保持目标、选择标记和操作提示可见且不越界。

### Bypass

```text
m -> target list -> Enter -> loading -> authority result
```

选择目标是必需动作参数，不属于风险二次确认。bypass 在目标确定后以 `confirmed=false` 立即发送，不显示
额外确认页；Service 的同 session、Candidate、revision、open state、CAS 与审计仍全部执行。

## 6. Textual TUI fallback

- Reviews 页使用相同 Snapshot 字段并增加 `m` binding；
- `ProposalMergeScreen` 只展示 authority 投影中、且当前 Snapshot 能解析到详情的最多 20 个目标；
- `↑/↓` 选择，Enter 文案明确为“合并”，该提交在需要确认的权限模式中构成一次显式确认；
- bypass 仍必须选择目标，但不增加第二个确认弹窗；
- 目标详情缺失、权限拒绝或无目标时失败关闭，不调用 Service；
- 提交时再次调用同一 PermissionChecker 和 Workbench Service，成功后重新读取权威 Snapshot；
- Reviews 异步详情刷新不得覆盖晚到的动作错误提示。

## 7. 并发、安全与审计

- Snapshot 投影与写入之间允许并发变化；最终 `validate_merge_target()` 和源状态 CAS 决定结果；
- 两个并发终态动作只能有一个获胜，失败方返回 conflict，不能覆盖已完成状态；
- target 保持 open，不复制或删除其内容；只有 source 写入 `state=merged` 与 `merged_into_id`；
- 成功恰好产生一条 `proposal.merged` 审计事件；重复请求按既有终态/CAS 规则收敛；
- 未提交的目标选择、索引、确认页和错误状态均为当前 UI 进程瞬态，不写 session resume 状态；
- 标题和 ID 在 TUI/Ink 输出前继续经过控制字符清理、长度限制和终端安全渲染。

## 8. 验收标准与证据

| 场景 | 预期 |
| --- | --- |
| 同 Candidate 较新 open revision | 按 revision 倒序进入最多 20 项的目标投影 |
| 关闭源、错误 Candidate、旧 revision 或关闭目标 | 不进入投影 |
| 空、自身、控制字符或超长目标 ID | Python/Node 协议失败关闭 |
| normal 模式 | 选择目标后一次明确确认再提交 |
| bypass 模式 | 选择目标后立即提交，无二次确认 |
| 目标在选择后失效 | Service 最终重验失败，不写入伪终态 |
| 真实 SQLite | source 变为 merged，target 保持 open，产生一条审计事件 |
| New UI | 目标导航、取消、无目标、bypass 和 completed result binding 通过 |
| Textual TUI | 目标弹窗、无目标失败关闭和权威快照刷新通过 |
| 80/120/200 列 | 目标列表和选中标记可见且无溢出 |

本切片只运行 Proposal governance、UI protocol/Bridge、Workbench Ink renderer/state 和 Textual
Workbench 的聚焦测试，不运行全量测试。

## 9. 非目标与后续

- waiting Approval 的 approve/reject 动作（后由 UI-10.6e 独立交付；Approval 不支持 defer/merge）；
- 跨 Candidate、跨 session、向旧 revision 或关闭 Proposal 合并；
- 批量 merge、自动选择目标或模型静默治理；
- HAR-09.6 before/after outcome tracking；
- UI-10.5 Timeline 与 revisioned domain patch；
- 任何代码执行、实验、promotion、rollback 或 Git 写入。

UI-10 与 HAR-09 仍为 `partial`。UI-10.6 的 Proposal approve/reject/defer/merge 与 Contract 显式转换
已经具备，waiting Approval 动作随后由 UI-10.6e 完成；其余 Outcome authority 以 HAR-09 模块册为准。
