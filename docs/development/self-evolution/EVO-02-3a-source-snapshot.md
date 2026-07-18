# EVO-02.3a Experiment Source Snapshot v1

## 目标

在任何 mutation planning 或文件写入之前，对隔离实验的源码、Harness Profile、Contract 执行配置和
允许工具生成可重建、可校验、无敏感信息的不可变身份。Snapshot 只把前置事实提升为
`source_ready=true`，仍固定 `execution_ready=false`。

本切片复用 Harness Profile loader 和 Runtime `ToolRegistry`，不复制配置解析或工具注册逻辑；不读取
API Key、环境变量值、用户名、主机名、绝对主工作区路径或文件正文。

## 捕获前置

`EvolutionExperimentSourceSnapshotBuilder` 仅接受：

1. 经过严格模型校验的 `EvolutionExperimentContract`；
2. 与 Contract 的 digest、session、mission、task 和 baseline 完全一致的 active Lease；
3. 位于引擎受管 worktree storage 中、目录名和 branch 均与 Lease 一致的精确 Git root；
4. HEAD 等于 Contract baseline，且 tracked/untracked 状态均为空；
5. Contract 声明的全部允许工具都以同名实例存在于当前 `ToolRegistry`。

任一条件漂移即 fail-closed，不生成降级 Snapshot。

## Snapshot 身份

- `baseline_commit`：Contract 的完整 SHA-1/SHA-256 commit；
- `baseline_tree`：该 commit 的 Git tree object ID；
- `baseline_tree_sha256`：对 `git ls-tree -r -z --full-tree` 原始字节计算 SHA-256，不保存路径清单；
- `profile_status/profile_sha256`：复用 Harness loader；合法 Profile 使用原文件摘要，明确缺失使用稳定
  sentinel 摘要，无效 Profile 拒绝；
- `experiment_config_sha256`：覆盖 scope、budget、allowed tools/checks、seed、网络/依赖安装和 static
  guard 前置，不包含凭据；
- 每个 Tool identity：真实类模块/限定名、NaumiAgent 版本、OpenAI tool schema 摘要和 `ToolMetadata`
  摘要；
- `toolset_sha256`：按工具名排序后的完整工具身份集合摘要；
- `snapshot_sha256/snapshot_id`：对上述全部绑定事实的 canonical JSON 计算完整 SHA-256，并派生
  `evs_` ID。

所有嵌套工具 identity、toolset 和最终 Snapshot 在反序列化时都会重新计算并使用 constant-time
compare 校验，不能只改字段后沿用旧 digest。

## 安全与可复现边界

- Git 调用不经过 shell，设置 `GIT_OPTIONAL_LOCKS=0`，15 秒超时，单次输出上限 64 MiB；
- 工作树 dirty、HEAD/branch/path 漂移时拒绝捕获，避免把 mutation 后状态冒充 baseline；
- Snapshot 不保存 `ls-tree` 内容、Profile 正文、tool description 之外的运行对象或任何 secret；
- Profile 缺失是显式且稳定的环境事实；Profile 存在但无效不是可接受降级状态；
- Tool identity 来自当前注册实例，不根据名称猜版本或能力；
- Snapshot 尚未持久化、运行检查或授权写入。

## 引擎组合

`AgentEngine.evolution_experiment_source_snapshot_builder` 复用引擎的唯一 `ToolRegistry` 和
worktree storage。Builder 在工具注册前构造但持有同一可变 registry，因此实际 capture 读取完成注册后的
权威工具集合。

## 验收证据

- 真实 Git + Candidate/Workbench approve + Contract + Lease + Source Snapshot 端到端通过；
- 相同 clean baseline 重复捕获得到完全相同 Snapshot；
- Git tree、有效/缺失 Profile、Contract 配置和五个允许工具均形成独立摘要；
- dirty worktree、branch 漂移、无效 Profile、缺失工具均机械拒绝；
- 最终 digest 被篡改后 Pydantic 反序列化拒绝；
- AgentEngine 组合测试确认 Builder 使用运行时同一基础设施；
- EVO-02.1/02.2、Worktree 和 Harness context 聚焦回归保持通过。

## 明确未包含

- Snapshot 持久化、UI、斜杠命令或 Agent Tool；
- EVO-02.5 patch writer、02.6 static guard、02.7 receipt；
- EVO-03 baseline/candidate checks 与比较；
- Profile trust 状态和模型 capability identity；它们属于 Harness Eval baseline，不应被本源码快照复制。

EVO-02.4a 已实现确定性不可执行 Mutation Plan，详见 `EVO-02-4a-mutation-plan.md`。下一切片应先补
EVO-02.6a Static Guard preflight，再进入受 Guard 约束的 Patch Writer。
