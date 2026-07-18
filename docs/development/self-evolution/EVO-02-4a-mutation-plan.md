# EVO-02.4a 不可执行 Mutation Plan v1

## 目标

把 approved Candidate、Experiment Contract、active Lease 与不可变 Source Snapshot 组合为确定性、
测试先行、预算受限的 Mutation Plan。Plan 只描述允许怎样推进，不写文件、不运行检查、不调用模型，
固定 `execution_ready=false`。

EVO-02.4 负责计划顺序和最小 approved scope；HAR-08/EVO-03 负责真正执行 baseline/candidate Eval 与
比较；EVO-02.6 负责 protected path、secret、generated/binary、symlink escape 和最终 diff budget
门禁。Planner 不复制后二者的裁决权。

## 权威绑定

`EvolutionMutationPlanner` 每次规划都重新验证：

1. Lease 为 active，且 Contract/Lease/Snapshot 的 ID、完整 digest 和 baseline 一致；
2. Source Snapshot 在规划开始前可从真实 worktree 重建且完全相等；
3. Candidate Store 当前 revision/digest、Proposal ID/kind 仍等于 approved Contract；
4. 规划结束后再次重建 Source Snapshot，扫描窗口内发生漂移则整份计划作废；
5. 每个目标都来自 Contract `allowed_files`，顺序和数量不改变，也不生成 scope 外目标。

Candidate 在批准后新增 Evidence/revision 时，旧 Contract 和旧 Snapshot 不能静默规划新内容。

## 真实文件事实

Planner 对每个 approved target 进行只读机械扫描：

- 路径必须留在 Lease worktree 内；
- 已存在目标必须是普通 UTF-8 文本，拒绝 symlink、目录、NUL/binary 和超过 2 MiB 的文件；
- 读取 baseline Git blob identity 和 blob bytes，并与 worktree 当前字节逐字节比较；
- 记录 file kind、size、content SHA-256、baseline blob 和 `modify/create` mode；
- baseline 不存在的 approved path 可标记为 `create`，但后续仍必须通过 Static Guard。

所有 Git 调用均不经过 shell，关闭 optional locks，限制超时和输出；Plan 不保存源码正文或绝对路径。

## 固定阶段

Plan 必须严格包含连续六阶段：

1. `inspect`：只用 Contract 的 `file_read/glob/grep` 定位直接相关修改点；
2. `baseline_check`：任何写入前，对全部 Contract metric 采集 RED/baseline 证据；
3. `mutation`：仅允许 `file_edit/file_write` 且 target 精确等于 approved scope；
4. `static_guard`：必须等待 EVO-02.6 的路径、受保护模块、secret、binary 与预算机械门禁；
5. `candidate_check`：使用与 baseline 完全相同的 metrics 采集 GREEN/candidate 证据；
6. `receipt`：只生成 diff/工具/检查引用，不自行宣称提升或推广。

模型校验强制 RED/GREEN metric 完全相同、阶段顺序不可变、mutation target 不扩张、写工具只来自固定
最小集合。

## 防篡改与预算

- Plan 不得放大 Contract budget，并按实际文件数、文件类型和 metrics 确定性收紧文件数、变更行数和
  工具调用上限；尝试次数继续受 Contract 上限约束；
- `unrelated_refactor_allowed=false`、`scope_expansion_allowed=false`；
- 网络与依赖安装继续为 false；
- `plan_sha256` 覆盖 Candidate objective、全部文件事实、阶段、预算和三重 provenance；
- `plan_id` 由完整 digest 派生，反序列化时重新计算并使用 constant-time compare。

相同可信输入和 baseline 产生完全相同 Plan；hypothesis、文件摘要、阶段或预算任一变化都会改变 identity。

## 引擎组合

`AgentEngine.evolution_mutation_planner` 复用现有 `EvolutionReviewService` 和
`EvolutionExperimentSourceSnapshotBuilder`。当前没有注册为 Agent Tool 或斜杠命令，避免在 Patch Writer
与 Static Guard 完成前出现半条执行旁路。

## 验收证据

- 真实 Git + Candidate/Workbench approve + Contract + Lease + Snapshot + Plan 端到端通过；
- 同一输入重复规划完全相等，真实 baseline blob、Python 类型和文件摘要进入 Plan；
- 六阶段顺序、RED/GREEN metrics、approved target、Contract budgets 与禁止项机械固定；
- Candidate revision 漂移、Source dirty、binary、symlink 和 manifest 篡改均拒绝；
- Snapshot 在扫描前后双重复核，目标 bytes 还与 baseline blob 再比较；
- AgentEngine 组合及 EVO-02.1..02.3、Worktree、Harness context 聚焦回归保持通过。

## 明确未包含

- Plan 持久化、用户 UI、斜杠命令或 Agent Tool；
- 任何文件写入、模型生成 patch、命令执行或依赖安装；
- EVO-02.5 Patch Writer、EVO-02.6 Static Guard、EVO-02.7 Mutation Receipt；
- HAR-08 Sandbox runner 与 EVO-03 baseline/candidate comparison。

EVO-02.6a Static Guard Preflight 与 EVO-02.5a 单文件原子 Patch Writer 已实现，分别详见
`EVO-02-6a-static-guard-preflight.md` 和 `EVO-02-5a-single-file-patch-writer.md`。下一切片需补持久 intent
journal 与崩溃恢复，不能把进程内回滚误称为多文件原子事务。
