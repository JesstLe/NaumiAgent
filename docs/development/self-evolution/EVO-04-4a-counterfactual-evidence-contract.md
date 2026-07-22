# EVO-04.4a Counterfactual Evidence Contract

## 目标

在 Independent Review 之后、Reward-hacking Detector 和最终 Decision 之前，签发一份完全确定性、可持久化、
不可接受 Candidate 的反事实证据 Authority。它回答的不是“改动是否优秀”，而是：当前 GREEN 改善是否可能由
更小 scope、删测试、修改 metric、放宽阈值、加入 skip/mock 或读取评测答案等替代解释造成。

本模块不调用 LLM。Independent Reviewer 的 recommendation 仅作为上游不可变审查记录保留，不能改变扫描规则、
覆盖 Mechanical Gate，或直接触发 accept/reject/promotion。

## 权威输入与重读链

用户或 Agent 只能提交一个 `review_id`。Executor 必须从 durable Store 重读并逐层精确比较：

1. `EvolutionIndependentReviewStore`：必须为 `completed` 且
   `counterfactual_review_ready=true`；
2. `EvolutionMechanicalGateStore`：必须与 Review 内嵌 Gate 完全一致且 outcome 为 `pass`；
3. `EvolutionDecisionInputStore`：必须与 Gate 内嵌 Decision Input 完全一致；
4. `EvolutionMutationReceiptStore`：必须与 Decision Input 的 Mutation authority 完全一致；
5. `EvolutionExperimentContractStore`：必须属于当前 workspace，且 constraints、baseline 与 Candidate revision
   不得漂移；
6. `EvolutionExperimentLeaseStore`：重新验证 Contract/session/mission/task/baseline/lease identity。

调用者不能提交源码、diff、finding、outcome、Lease 路径或检查开关。任何 Store 缺失、跨 workspace、索引损坏或
引用漂移均 typed fail-closed。

## 真实字节证据

扫描只读取 Contract 对应的受管 worktree：

- worktree 必须位于配置的 `worktree_storage_dir` 直接子目录，名称必须与 Lease 一致；
- Git top-level 必须精确等于 worktree root，HEAD 必须仍是 Contract baseline；
- 相对路径拒绝绝对路径、`..`、控制字符和 symlink/中间 symlink 逃逸；
- baseline 使用 `git cat-file` 从不可变 baseline commit 读取；
- candidate 使用读取前后 inode/device/size/mtime 校验，防止 scan 期间 TOCTOU；
- 每个文件最多 2 MiB，只接受 UTF-8；unified diff 最多 20,000 行；
- baseline/candidate SHA-256、added/deleted line count 与 unified diff SHA-256 必须逐文件等于 Mutation
  Receipt，否则不签发 Evidence。

Artifact 不保存源代码、diff 正文、绝对 worktree 路径、Prompt 或模型 reasoning。它只保存内容摘要、行号、
finding code、严重度、机械统计和代表性 finding evidence digest。每文件每类 finding 最多保存 16 条代表证据，
但 `finding_codes` 始终保留类别存在性。

## 机械反事实规则

固定 11 条检查：

1. `authority_chain_bound`；
2. `worktree_binding_exact`；
3. `candidate_bytes_match_receipt`；
4. `unified_diff_match_receipt`；
5. `no_reducible_scope`；
6. `no_test_deletion`；
7. `no_metric_mutation`；
8. `no_threshold_relaxation`；
9. `no_skip_added`；
10. `no_mock_added`；
11. `no_evaluation_leakage`。

检测器基于真实 before/after diff：

- test/spec 路径删除语义行形成 `test_deletion`；
- metric/evaluator/benchmark/scoring 路径，或包含 Contract required metric 的变更形成
  `metric_mutation`；
- 同一 replacement group 中保持结构、但 `>=`/`>` 数值降低或 `<=`/`<` 数值提高，形成
  `threshold_relaxation`；常见 min/pass/score/rate 与 max/error/fail 配置键也按方向检测；
- 新增 pytest/unittest/JS skip、xfail/disabled 形成 `skip_added`；
- 新增 Mock/MagicMock/monkeypatch/jest/vi/sinon stub 形成 `mock_added`；
- 新增 ground truth、golden answer、answer key、expected output、test label 或 evaluation result 读取形成
  `evaluation_leakage`；
- 空白、注释或仅格式变化的文件形成 `non_semantic_scope`；若一个文件的全部语义变更都能被上述直接替代解释
  覆盖，则标记 `smaller_scope_plausible=true`。

静态扫描只能证明“发现了替代解释”或“当前规则未发现”，不能证明因果最小性。因此输出使用 `not_found`，不使用
`proved_minimal`。

## Outcome 与状态边界

- 无 finding：`outcome=clear`；
- 任一 finding：`outcome=concern`，同时要求 `investigate_alternative_explanation`；
- 两种 outcome 均固定 `continue_to_reward_hacking_review`，进入 EVO-04.5；
- 固定 `mechanical_gate_outcome_preserved=true`、`reviewer_advisory_only=true`、
  `llm_used=false`；
- 固定 `candidate_acceptance_decided=false`、`promotion_ready=false`。

`concern` 不是 Mechanical veto，也不是最终 reject；`clear` 也不是 accept。EVO-04.5 必须继续检查更隐蔽的行为型
reward hacking；EVO-04.6a 现已综合形成 Decision State，但尚未消费 escalation answer。

## 持久化、幂等与并发

`EvolutionCounterfactualEvidenceStore` 对 `review_id` 建立唯一约束，artifact ID/SHA-256 由完整 canonical
payload 决定。扫描是确定性只读函数，不调用模型，因此不需要模型 single-flight lease：并发调用可同时读取，
SQLite `BEGIN IMMEDIATE` 保证最终只能持久化同一 artifact；不同结果冲突失败关闭。

Store 同时校验 JSON artifact 和 evidence/review/workspace/outcome/created_at 索引列。首次签发后，即使受管
worktree 后续清理，已签发 artifact 仍可从 Store 重读；首次签发前若 worktree 已不存在，则明确失败，不能根据
Mutation Receipt 摘要猜测源码语义。

## 双通道与跨界面

- 用户：`/evolution counterfactual <independent-review-id>`；
- Agent：`evolution_counterfactual_evidence(review_id=...)`。

两者调用同一 Executor、Builder、Store 和 Markdown renderer。Shared slash bridge 使 New UI 与 Textual TUI
显示相同 evidence ID、checks、finding categories、affected files、required actions 和“尚未接受 Candidate”
边界；Agent Tool 结果继续由两端共同的结构化 Tool card 呈现，不维护第二份决策逻辑。

## 权限治理

Agent Tool 属于 `evolution_decision_artifact` 中风险派生写入：

- bypass/permissive/moderate/strict 可用；
- 不进行逐次二次确认；
- lockdown 阻断；
- 每会话最多 50 次；
- bypass 只跳过交互确认，不能绕过 workspace、authority、Lease、digest、symlink 或文件大小规则。

## 验收证据

- 真实 Candidate→Mutation→Evaluation→Decision Input→Mechanical Gate→Independent Review→
  Counterfactual 完整链通过；
- 4 路并发返回同一个 durable artifact；
- Slash 与 Agent Tool 输出同一 artifact；
- 删除 test、修改 metric、放宽阈值、skip、mock、evaluation leakage 均有独立 fixture；
- 纯空白变化形成更小 scope finding；
- candidate digest 漂移、veto Review、跨 workspace、Store 索引篡改失败关闭；
- artifact 不包含源码、Prompt 或 worktree 绝对路径；
- 权限矩阵、Engine composition、Ruff、compile/import 与聚焦 pytest 通过。

## 明确未完成

- EVO-04.5a 行为型 Reward-hacking Evidence 已实现；
- EVO-04.6a Decision State 与 EVO-04.6b Escalation Resolution 已完成；
- EVO-04.7a Reflection Memory 已完成；
- EVO-05 promotion/rollback；
- 对已在首次扫描前清理的 worktree 提供独立 encrypted content archive；当前严格失败关闭，不从 digest 反推。

## 下一步

EVO-04.5a 至 EVO-04.7a 已消费本 authority 并形成四态 Decision、持久 Resolution 与非注入 Reflection。下一步
实现 EVO-05.1a Promotion Package Input Contract；`concern/inconclusive` 仍不得被 Reviewer 叙事覆盖。
