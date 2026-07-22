# EVO-02.1b Durable Experiment Contract Authority

## 目标

EVO-02.1a 的 `EvolutionExperimentContract` 已确定性冻结 approved Proposal 的 baseline、scope、budget、allowed
tools/checks、network 与 dependency constraints，但只作为调用链中的对象传递。后续 Mutation Receipt、Validation
Plan 与 Final Evaluation Receipt 只保存 Contract ID/digest，EVO-04 无法在决策时独立重读完整用户约束。

本切片把每次成功签发的 Contract 包装为 workspace-bound、防篡改、不可变的 durable authority。Issuer 只有在
authority 成功写入 Store 后才返回 Contract；调用方不能再生成“已签发但无法重读”的实验约束。

## Authority envelope

`EvolutionExperimentContractAuthority` 完整嵌入并重新验证 Contract，同时签名投影：

- canonical workspace root；
- Contract ID 与 manifest digest；
- Candidate ID/revision；
- human reviewer 的 approval timestamp；
- Contract 全部 source、baseline、scope、budget、allowed tools/checks 和禁止能力。

envelope 使用 canonical JSON + SHA-256 生成 `evxauth_*` identity。workspace、Contract、Candidate、approval 或
任一约束发生变化都会得到不同 identity；手工修改 projection、embedded Contract 或 digest 会在模型验证时失败。

没有把 workspace 追加进历史 Contract v1 schema，因为这会改变所有既有 Contract/Lease/Plan/Receipt digest。
workspace binding 位于新的 authority envelope 中，既保留旧 artifact 可读性，又为 EVO-04 提供签名后的工作区边界。

## Durable Store

`EvolutionExperimentContractStore` 使用 Session/Evolution SQLite 中的 `evolution_experiment_contracts` 表：

- `(workspace_root, contract_id)` 是不可变主键；
- authority ID 全局唯一，并包含 workspace binding；
- `BEGIN IMMEDIATE` 串行化并发重复签发，相同内容幂等返回；
- 同一 workspace/Contract ID 的不同内容拒绝覆盖；
- row columns、typed envelope、embedded Contract 与两个 digest 在每次读取时全部复验；
- payload 上限 512 KiB，异常以稳定中文错误和 error code 失败关闭；
- 相同 Contract ID 在其他 workspace 不会被当前工作区读取。

Store 不保存源码、diff、Prompt、模型输出、凭据或环境变量。

## Issuer 与组合根

`EvolutionExperimentContractIssuer` 现在强制依赖 Store；不允许传入 `None` 或退回内存-only 模式。签发顺序固定为：

1. 重验 approved Workbench Proposal 与当前 Candidate Preview；
2. 只读捕获精确 Git baseline；
3. 应用 risk budget cap 并构建 Contract v1；
4. 建立 workspace-bound authority；
5. durable commit 成功后返回 embedded Contract。

`AgentEngine` 在 Session SQLite 上组合唯一 Store，并供后续 EVO-04 Decision Input executor 重读。

## 双通道检查

- 用户：`/evolution experiment-contract <contract-id>`；
- Agent：`evolution_experiment_contract_authority(contract_id=...)`。

两条通道只读同一个 Store 和 renderer，展示批准者、scope、文件、预算、checks、network/dependency 禁止状态与
authority digest；它们不重新签发 Contract，也不授予执行或 promotion 权限。

## 聚焦验证

- 真实临时 Git 仓库、Candidate SQLite、Workbench SQLite、Proposal Queue 与 human approve 端到端签发；
- 四个并发重复签发收敛到同一 Contract/authority；
- Store 重读、workspace 隔离、embedded Contract 等值与 `evxauth_*` identity；
- SQLite manifest column 篡改后读取以 `experiment_contract_authority_store_corrupt` 失败关闭；
- slash 与 Agent Tool 均从 Store 重读相同 authority；
- 原有 Contract→Worktree Lease 路径继续使用真实 baseline 且保持主工作树不变；
- open/stale Proposal、预算扩张、nested repo 与 manifest 篡改仍被既有门禁拒绝。

## 明确未完成

- 本切片不新增 Proposal approve/Contract issue UI；签发仍由受治理 workflow 触发，新增通道只负责审查 durable
  authority。
- EVO-04.1 仍需同时从 Candidate Store、Mutation Receipt Store、Experiment Contract Store 和 Final Evaluation
  Receipt Store 重读并交叉验证完整 Decision Input。
- mechanical gate、reviewer、reward-hacking detector、decision state 与 promotion 均未实现。

## 下一步

现在可以实现 EVO-04.1a Decision Input Contract：只接收 workspace、Final Evaluation Receipt ID 和必要 artifact
ID，从四个 Store 重读完整 authority，验证 Candidate revision/risk、Mutation files/scope、Experiment constraints
与 Final Evaluation Plan/Candidate 全部一致；仍不做 accept/reject。
