# HAR-09.5c Approved Proposal 显式签发 Experiment Contract

## 交付目标

关闭 `Feedback → Candidate → Proposal → human approve → Experiment Contract Authority` 的产品断点。
用户在 Workbench Reviews 中批准 Evolution Proposal 后，必须再执行一次独立、可审计的“签发契约”动作；
Agent 也可通过同一 issuer 发起该动作。签发只冻结实验约束，不修改代码、不获取 Worktree Lease、不运行检查，
也不授予 promotion 权限。

该切片复用 EVO-02.1b 的 Contract/Authority 和 HAR-09.5b1 的 Proposal 治理事实，不复制 Candidate、Proposal
或实验状态机。

## 权威链路

1. 只接受当前 workspace、当前 session 中 `approved + evolution_candidate` 的 Proposal。
2. Issuer 重新读取 Candidate/Preview，验证 revision、digest、proposal kind、scope 和 human approval。
3. 使用 Proposal ID 派生稳定 63-bit seed；调用方不能通过反复传不同 seed 制造多份 Authority。
4. Store 在 `BEGIN IMMEDIATE` 中按 `(workspace_root, source_session_id, workbench_proposal_id)` 单飞。
5. 首次调用持久化完整 workspace-bound Authority；重复或并发调用返回同一 Contract。
6. 返回值固定 `execution_ready=false`、`promotion_ready=false`；后续 Lease、Mutation、Evaluation 仍需各自 gate。

历史数据库在首次访问时增加 source session/proposal 投影，并从已签名 authority JSON 回填。唯一索引建立前会
验证数据；冲突或篡改以 Store corruption 失败关闭，不静默覆盖或任选一条。

## 双通道与权限

- 用户通道：New UI/TUI Workbench Reviews 选中 approved Evolution Proposal，按 `c` 签发或重开。
- Agent 通道：`evolution_issue_experiment_contract(proposal_id=...)`。
- 审查通道：既有 `/evolution experiment-contract <contract-id>` 与只读 Agent Tool 保持不变。

`evolution_issue_experiment_contract` 是显式高风险权限：strict/moderate/permissive/default 需要一次确认；
`bypass` 是全权限模式，直接执行且不出现二次确认。所有模式都不能跳过 Proposal/Candidate/Store 验证。

## Bridge 与公开投影

复用 `workbench/proposal/action`，新增 action `issue_contract`，不增加第二套 Workbench action transport。
该 action 禁止携带 `decision_note`；Bridge 强制当前 session 并返回原 action result 以及有界公开投影：

- Authority/Contract ID 与两个 SHA-256；
- Proposal、Candidate ID/revision；
- impact scope、1..16 个安全相对文件路径；
- bounded budget；
- 固定为 false 的 execution/promotion readiness。

Node 协议拒绝绝对路径、`..`、重复路径、超出数量、非法 digest、越界预算和任何 readiness 提权。

## New UI 与 TUI

- Reviews 的 actionable 集合现在包含 waiting Approval、open Proposal，以及待签发的 approved Evolution Proposal。
- open Proposal 仍使用 `a/x`；approved Evolution Proposal 只显示 `c`，避免误导用户可以重复 approve。
- normal 模式展示“只冻结约束”的独立确认；bypass 直接发送。
- New UI 显示 Contract ID、Authority ID 和 `execution_ready=false`。
- TUI 重新读取同一 Workbench Snapshot，并用持久化回执明确“尚未修改代码或批准发布”。
- 选择、确认态和回执卡仍是进程内状态；新会话不会恢复上次未提交输入。

## 聚焦验收

- 真实 Git/Candidate/Workbench SQLite 链路完成 approved Proposal → durable Authority，主工作树零变更。
- 五路不同 seed 并发签发收敛到同一 Contract；后续任意 seed 重试仍返回同一 Authority。
- legacy 表自动增加并回填 source projection，重读结果与原 Authority 完全相同。
- Bridge normal 模式先返回 `needs_confirmation`，确认后返回严格公开投影和新 Snapshot。
- PermissionChecker、Agent Tool、Python 协议、Node 协议/状态/80-200 列渲染、Textual normal/bypass 均有定向测试。
- 本切片不运行全量测试，只运行相关小模块。

## 明确未完成

- Proposal `defer/merge` 的终端表单与 waiting Approval 动作；
- Contract → Lease/Mutation 的用户动作和执行进度 UI；
- HAR-09.6 before/after outcome tracking；
- UI-10.5 Timeline 中的 Contract domain event；
- EVO-04 最终 decision 与 EVO-05 promotion。

这些能力必须继续消费 durable Authority，不能把 `approved` 或 `contract` 解释为执行许可。
