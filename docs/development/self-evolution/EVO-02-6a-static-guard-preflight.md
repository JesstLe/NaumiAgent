# EVO-02.6a Static Guard Preflight v1

## 目标

在 Patch Writer 获得任何写入能力前，对“具体目标路径 + 完整提议内容”执行确定性机械审查，并生成
防篡改、可复核但仍不可执行的 Guard Receipt。Guard 独立于交互权限模式；即使 runtime 是 bypass，
也不能覆盖 protected、scope、secret、binary、generated、symlink 或预算违规。

本切片不写文件、不运行命令、不调用模型，也不把 `preflight_passed` 等同于写入授权。
`write_authorized=false`、`execution_ready=false` 始终成立。

## 权威绑定

`EvolutionStaticGuard.preflight()` 必须同时绑定：

- 当前 `EvolutionExperimentContract`；
- active、同 Contract 的 `ExperimentWorktreeLease`；
- 同 Lease/baseline 的 `EvolutionExperimentSourceSnapshot`；
- 同 Contract/Lease/Snapshot 且要求 static guard 的 `EvolutionMutationPlan`；
- 本次待审查的完整 `path → str/bytes` 内容集合。

Guard 在内容扫描前后各重建一次 Source Snapshot。任何 HEAD、branch、dirty、Profile、Tool identity 或
路径漂移都写入 `source_drift`，不能产出通过结论。

## 路径门禁

版本化 `EvolutionStaticGuardPolicy` 机械拒绝：

- Contract/Plan scope 外路径；
- `..`、绝对路径、控制字符和 worktree escape；
- 目标或任一父目录为 symlink；
- safety、credential storage、migration、release/update、permission port、engine、Tool base 及当前
  Evolution governance contract/lease/snapshot/plan/guard 等受保护模块；
- GitHub release workflows 与 build/package/release scripts；
- dependency manifests 和 lockfiles；
- `build/dist/generated/node_modules/.venv`、generated/minified 文件名。

Policy 的完整规则集合、大小限制和 secret policy version 进入 `policy_sha256`。bypass 不参与计算。

## 内容门禁

- 单文件最多 2 MiB，一次最多 16 个文件；
- NUL 或非 UTF-8 内容拒绝；
- 现有文件或提议内容出现 `@generated`、`Code generated ... DO NOT EDIT` 等标记时拒绝；
- 私钥头、AWS/GitHub/Slack/OpenAI/Bearer 已知 token 格式机械拒绝；
- `api_key/token/password/secret/credential` assignment 使用长度、placeholder 过滤与 entropy 共同判定；
- `{env:BRAVE_SEARCH_API_KEY}` 等环境引用不会被当作 secret；
- Receipt 永不保存正文，只保存 before/after SHA-256、size、operation 和增删行计数。

Secret 检测不是通用凭据证明器；它是 fail-closed 的已知格式/高熵 assignment v1。后续可以增加
provider-specific detector，但不得把 secret 原文写入错误、Receipt 或审计。

## Baseline 与预算

- 当前文件摘要必须等于 Mutation Plan 的 baseline fact；`create` 目标当前已存在时拒绝；
- 内容与 baseline 完全相同产生 `no_changes`；
- `SequenceMatcher` 对普通文本计算精确 added/deleted lines；超过 10,000 行时使用保守全量计数，避免
  Guard 自身遭受高复杂度 diff 资源耗尽；
- 文件数和总 changed lines 分别受 Mutation Plan 收紧预算约束；
- scope expansion 文件仍形成脱敏 change fact 与明确 violation，但不能通过。

## Receipt

`EvolutionStaticGuardReceipt` 包含：

- policy/Contract/Lease/Snapshot/Mutation Plan provenance；
- 排序后的 change facts 与 `changes_sha256`；
- 去重、稳定排序的 typed violations；
- 文件数、变更行数、`preflight_passed`；
- `bypass_can_override=false`、`write_authorized=false`、`execution_ready=false`；
- canonical `receipt_sha256` 与派生 `evg_` ID。

反序列化会重新验证 change digest、总数、passed/violations 和最终 identity。修改任一字段后沿用旧摘要
会被 Pydantic 拒绝。

## 引擎组合

`AgentEngine.evolution_static_guard` 复用唯一 Source Snapshot Builder。当前未注册斜杠命令或 Agent Tool，
因为用户和 Agent 都不应在 Patch Writer 存在前拿到一个看似“已批准写入”的半成品动作。

## 验收证据

- 真实 Candidate→Proposal approve→Contract→Lease→Snapshot→Plan→Guard 端到端通过；
- 两路并发审查同一安全内容得到完全相同 Receipt，主工作树与隔离 worktree 字节均不变；
- hardcoded secret、generated marker、binary、scope/file/line budget expansion 均返回 typed block；
- Receipt JSON 不含测试 secret 或提议正文；
- protected/dependency/generated path policy 与安全环境变量引用分别验证；
- symlink escape 同时报告 source drift、path escape、symlink 和 baseline mismatch；
- Receipt 摘要篡改被拒绝；EVO-02.1..02.4、Worktree、Engine composition 聚焦回归保持通过。

## 当前不足与后续

- 旧 `self_modify`/`hotreload` 仍保留其历史保护清单；新自进化 Patch Writer 必须只使用本 Guard，后续再
  单独统一 legacy 路径，不能在本切片混改旧执行语义；
- Policy 当前随代码发布，尚未成为签名、可迁移的持久策略 artifact；ARC-05/07 阶段需要补版本迁移和
  发布签名；
- 本切片不判断修改是否语义正确，也不执行 HAR-08/EVO-03；
- EVO-02.5a/2.5b/2.5c 已实现受 Guard Receipt 约束的单/多文件原子 Writer 和持久崩溃恢复；
  EVO-02.6b 已补齐完整 Diff/API Postflight；EVO-02.7a 已实现 Mutation Receipt Core，2.7b1 已实现
  隔离内存 Mutation Generation Trace，2.7b2 已补 Static Guard Receipt v2 的 Trace 强绑定。

Writer 会在持锁后重跑本 preflight 并要求 Receipt 完全一致；写后再核对文件摘要与 Git scope，失败则在
隔离 worktree 内回滚，强制终止窗口由持久 journal 在启动时恢复。详见
`EVO-02-5a-single-file-patch-writer.md`、`EVO-02-5b-patch-journal-recovery.md` 和
`EVO-02-6b-postflight-diff-api-guard.md`。
