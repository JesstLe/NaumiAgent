# EVO-05.3b2b Fenced Three-way Rebase

## 1. 目标

当审批后的 target 仅发生线性前进时，把已批准 Candidate 的真实文件字节安全重放到 current target 的一次性 detached
worktree。该切片执行 Git 三方合并并持久化结果，但不运行验证、不修改用户工作区/target branch，也不授予 promotion authority。

## 2. 输入 authority

执行器只接受 EVO-05.3b2a 证明的 `advanced + target_only_stale + execution_eligible` Request，并再次绑定：

- immutable Promotion Package Input 与 patch manifest；
- active、未过期且属于受管目录的 Experiment Lease；
- Request baseline、原 target head 与当前线性后代 head；
- 除 target movement 外仍 current 的 Approval、Principal、Signature、Input 与 Reflection authority。

`same` 仍由 EVO-05.3b1 exact replay 处理；`diverged`、unavailable、Lease 过期或非 target authority 漂移均 fail closed。

## 3. 真实三方重放

`EvolutionRevalidationExecutionService` 在一次 authority snapshot 中分派 exact replay 或 advanced rebase。Advanced 路径：

1. 从 active Candidate worktree 按批准 digest 重读真实 baseline/Candidate bytes；
2. 在 current target head 创建 disposable detached worktree；
3. 对每个批准文件执行 `current / baseline / candidate` 三方合并；
4. target 未改动时直接写 Candidate；内容已等价时保持不变；独立文本变化调用 `git merge-file --diff3`；
5. create 冲突、删除、文件类型/大小异常、执行位漂移或同区块修改形成结构化 conflict，不猜测解决；
6. 复核 resulting status 只能包含批准 patch manifest 中的路径；
7. 无论成功、冲突或失败都强制清理 detached worktree，并复核 source、main worktree 与 target branch 未变。

## 4. Claim、fencing 与恢复

SQLite 以 `(request_id, current_target_head)` 为幂等键保存 attempt。`BEGIN IMMEDIATE` 串行签发随机 owner token 与单调 epoch：

- 未过期 active claim 阻断并发执行；
- claim 过期后新执行者递增 epoch，清理由确定性前缀定位的遗留 worktree；
- 实际 epoch worktree 路径通过 CAS 绑定到 durable claim；
- terminal Outcome 对同一 Request/target 幂等返回；
- 旧 owner/epoch 无法提交 Outcome，即使它在 lease 过期后继续运行。

## 5. Durable Outcome

`EvolutionRevalidationRebaseOutcome` 使用 `evolution-revalidation-rebase-v1`，保存 request/input/lease、原 target、current target、
epoch、逐文件 baseline/target/Candidate/result digest、merge strategy、冲突路径、隔离不变式与 failure code。Artifact 采用规范化
JSON SHA-256 派生 identity，并在读取和渲染前重新验证。

状态语义：

- `succeeded`：三方结果完整、作用域精确、四项隔离不变式均为真；
- `conflicted`：存在结构化冲突且未写入部分合并结果；
- `failed`：authority、恢复、Git、清理或隔离不变式失败，并持久化机械 failure code。

所有状态都固定 `validation_executed=false`、`promotion_authority=false`。Success 只是下一步 Harness revalidation 的输入。

## 6. 用户与 Agent 表面

既有命令和 Tool 保持单入口：

```text
/evolution revalidation-replay <revalidation-request-id>
evolution_revalidation_replay(request_id=...)
```

输出会按 current target 关系展示 Exact Replay Receipt 或 Rebase Outcome，包含状态、target 转移、epoch、文件/冲突与权限边界。

## 7. 验收证据

- current target 独立新增文件时，Candidate 在 detached worktree 成功重放，source/main/target 均不变；
- target 与 Candidate 修改不同文本区块时，真实 `git merge-file` 同时保留两方变化；
- 同区块修改形成 durable conflict，不落地部分结果；
- active claim 阻断并发重复执行；完成后的重复调用幂等返回同一 terminal artifact；
- claim 过期后清理遗留 worktree、epoch 增长，旧 executor 被 fencing；
- Candidate 源消失也形成 durable failed Outcome；
- Tool 与 `/evolution revalidation-replay` 均可展示 advanced Outcome；
- 仅运行本模块及相邻 Request/Exact Replay 测试，不运行全量测试。

## 8. 当前边界与后续

- Candidate 源仍依赖 active Experiment Lease/worktree；要支持跨长期重启恢复，需要后续增加不可变、加密且受保留策略治理的
  Candidate blob authority，不能把临时 worktree 当永久归档。
- [EVO-05.3c](EVO-05-3c-harness-revalidation-evidence.md) 已消费成功 Outcome，在同一 result identity 上运行
  Harness Profile checks 并签发新 Worker evidence。
- EVO-05.3d 必须使旧 validation/decision evidence 明确 stale，并形成可审计 Revalidation Outcome；在此之前禁止 promotion。
